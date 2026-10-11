"""Runtime configuration, resolved from environment variables and an optional `.env` file."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# USD per 1M tokens: (input, cached input, output), OpenAI standard tier, short context, as of Oct 2026.
# Used only for cost reporting and the app's budget guard.
MODEL_PRICES_PER_MTOK: dict[str, tuple[float, float, float]] = {
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-6.1-sol": (2.00, 0.10, 10.00),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.2": (1.75, 0.175, 14.00),
    "gpt-5.1": (1.25, 0.125, 10.00),
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    zip_path: Path = PROJECT_ROOT / "contracts.zip"
    data_dir: Path = PROJECT_ROOT / "data"
    eval_dir: Path = PROJECT_ROOT / "eval"

    ocr_dpi: int = 300
    ocr_workers: int = Field(default_factory=lambda: max(1, (os.cpu_count() or 2) - 2))
    # Pages whose native text layer is shorter than this (in non-space characters) are OCR'd.
    ocr_min_native_chars: int = 80

    colbert_model: str = "lightonai/answerai-colbert-small-v1"
    retrieval_mode: str = "plaid_rerank"
    chunk_max_tokens: int = 230
    chunk_overlap_tokens: int = 40

    openai_api_key: SecretStr | None = None
    # Escalation re-reads flagged documents with more reasoning effort. The judge only grades free-text fields and
    # chat answers (everything else is scored deterministically); point JUDGE_MODEL at a different model family
    # when quota allows so those grades are not self-assessed.
    extraction_model: str = "gpt-5-mini"
    extraction_reasoning_effort: str = "medium"
    escalation_model: str = "gpt-5-mini"
    escalation_reasoning_effort: str = "high"
    router_model: str = "gpt-5-mini"
    router_reasoning_effort: str = "low"
    chat_model: str = "gpt-5-mini"
    chat_reasoning_effort: str = "low"
    judge_model: str = "gpt-5-mini"
    judge_reasoning_effort: str = "medium"
    extraction_concurrency: int = 6
    # Client-side pacing (requests/min, tokens/min) per model, set to this account's OpenAI usage tier.
    # Override with LLM_RATE_LIMITS='{"gpt-5-mini": [450, 450000]}' after a tier upgrade.
    llm_rate_limits: dict[str, tuple[int, int]] = {
        "gpt-6-luna": (9, 55_000),
        "gpt-5-mini": (45, 55_000),
        "gpt-5-nano": (45, 36_000),
    }
    llm_default_rate_limit: tuple[int, int] = (9, 36_000)
    # Documents longer than this many characters get retrieval-scoped context instead of full text.
    full_context_max_chars: int = 160_000
    max_escalation_share: float = 0.35

    app_username: str | None = None
    app_password: SecretStr | None = None
    # Bearer token for the remote MCP server used by Claude Tag. Unset locally; set on Fly.
    mcp_token: SecretStr | None = None
    max_questions_per_minute: int = 6
    max_question_chars: int = 600
    daily_llm_budget_usd: float = 5.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "contracts.db"

    @property
    def pdf_dir(self) -> Path:
        return self.data_dir / "pdfs"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"

    @property
    def llm_cache_dir(self) -> Path:
        return self.data_dir / "cache" / "llm"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def tessdata_dir(self) -> Path:
        return self.data_dir / "tessdata"

    @property
    def ground_truth_dir(self) -> Path:
        return self.eval_dir / "ground_truth"

    @property
    def eval_results_dir(self) -> Path:
        return self.eval_dir / "results"

    def require_openai_key(self) -> str:
        if self.openai_api_key is None or not self.openai_api_key.get_secret_value().strip():
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to .env locally or as a Space secret when hosted."
            )
        return self.openai_api_key.get_secret_value().strip()

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.pdf_dir,
            self.processed_dir,
            self.index_dir,
            self.llm_cache_dir,
            self.exports_dir,
            self.eval_results_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def model_price(model: str) -> tuple[float, float, float] | None:
    """Return per-1M-token prices for a model, matching dated snapshots to their base name."""
    if model in MODEL_PRICES_PER_MTOK:
        return MODEL_PRICES_PER_MTOK[model]
    for name in sorted(MODEL_PRICES_PER_MTOK, key=len, reverse=True):
        if model.startswith(name + "-20") or model.startswith(name + "-2"):
            return MODEL_PRICES_PER_MTOK[name]
    return None
