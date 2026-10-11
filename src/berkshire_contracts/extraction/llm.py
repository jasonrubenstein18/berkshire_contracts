"""OpenAI Responses API wrapper: strict structured outputs, response caching, cost and latency logging."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from openai import APIConnectionError, APIStatusError, OpenAI, RateLimitError
from pydantic import BaseModel
from sqlalchemy import func
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from berkshire_contracts.config import Settings, get_settings, model_price
from berkshire_contracts.db import LlmCall, session_scope

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LlmError(RuntimeError):
    pass


class BudgetExceeded(LlmError):
    pass


@dataclass
class CallStats:
    model: str
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0
    latency_ms: int = 0
    cache_hit: bool = False


def estimate_cost(model: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float | None:
    prices = model_price(model)
    if prices is None:
        return None
    price_in, price_cached, price_out = prices
    uncached = max(0, input_tokens - cached_tokens)
    return round((uncached * price_in + cached_tokens * price_cached + output_tokens * price_out) / 1e6, 6)


class RateLimiter:
    """Sliding 60-second window over requests and estimated tokens, shared by every thread in the process."""

    def __init__(self, rpm: int, tpm: int) -> None:
        self.rpm, self.tpm = rpm, tpm
        self._events: deque[tuple[float, int]] = deque()
        self._cond = threading.Condition()

    def acquire(self, tokens: int) -> float:
        tokens = min(tokens, self.tpm)
        waited = 0.0
        with self._cond:
            while True:
                now = time.monotonic()
                while self._events and now - self._events[0][0] >= 60:
                    self._events.popleft()
                used = sum(t for _, t in self._events)
                if len(self._events) < self.rpm and used + tokens <= self.tpm:
                    self._events.append((now, tokens))
                    return waited
                pause = max(0.05, 60.05 - (now - self._events[0][0])) if self._events else 0.5
                self._cond.wait(timeout=pause)
                waited += pause


_LIMITERS: dict[str, RateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def limiter_for(model: str, settings: Settings) -> RateLimiter:
    with _LIMITERS_LOCK:
        if model not in _LIMITERS:
            rpm, tpm = settings.llm_rate_limits.get(model, settings.llm_default_rate_limit)
            _LIMITERS[model] = RateLimiter(rpm, tpm)
        return _LIMITERS[model]


def estimate_request_tokens(*texts: str, output_allowance: int = 1500) -> int:
    return int(sum(len(t) for t in texts) / 3.5) + output_allowance


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, APIConnectionError):
        return True
    if isinstance(exc, RateLimitError):
        message = str(exc).lower()
        return "per day" not in message and "insufficient_quota" not in message and "credits" not in message
    return False


def spend_today(purposes: tuple[str, ...] | None = None) -> float:
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    with session_scope() as s:
        q = s.query(func.coalesce(func.sum(LlmCall.cost_usd), 0.0)).filter(
            LlmCall.created_at >= start, LlmCall.cache_hit.is_(False)
        )
        if purposes:
            q = q.filter(LlmCall.purpose.in_(purposes))
        return float(q.scalar() or 0.0)


class LlmClient:
    def __init__(self, settings: Settings | None = None, log_calls: bool = True) -> None:
        self.settings = settings or get_settings()
        self._client = OpenAI(api_key=self.settings.require_openai_key(), max_retries=2, timeout=240)
        self.log_calls = log_calls
        self._lock = threading.Lock()
        self.settings.llm_cache_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def cache_key(payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()

    def _log(
        self,
        purpose: str,
        stats: CallStats,
        prompt_version: str | None,
        file_id: str | None,
        error: str | None = None,
    ) -> None:
        if not self.log_calls:
            return
        with self._lock, session_scope() as s:
            s.add(
                LlmCall(
                    purpose=purpose,
                    model=stats.model,
                    prompt_version=prompt_version,
                    file_id=file_id,
                    input_tokens=stats.input_tokens,
                    cached_tokens=stats.cached_tokens,
                    output_tokens=stats.output_tokens,
                    cost_usd=stats.cost_usd,
                    latency_ms=stats.latency_ms,
                    cache_hit=stats.cache_hit,
                    error=error,
                )
            )

    @retry(
        retry=retry_if_exception(_retryable),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _call(self, **kwargs: Any) -> Any:
        tokens = estimate_request_tokens(kwargs["instructions"], kwargs["input"])
        limiter_for(kwargs["model"], self.settings).acquire(tokens)
        return self._client.responses.parse(**kwargs)

    def parse(
        self,
        *,
        purpose: str,
        model: str,
        instructions: str,
        user_input: str,
        schema: type[T],
        prompt_version: str | None = None,
        file_id: str | None = None,
        reasoning_effort: str | None = None,
        max_output_tokens: int = 16000,
        use_cache: bool = True,
        budget_usd: float | None = None,
    ) -> tuple[T, CallStats]:
        payload = {
            "model": model,
            "instructions": instructions,
            "input": user_input,
            "schema": schema.model_json_schema(),
            "reasoning": reasoning_effort,
            "prompt_version": prompt_version,
        }
        key = self.cache_key(payload)
        cache_path = self.settings.llm_cache_dir / f"{key}.json"
        if use_cache and cache_path.exists():
            cached = json.loads(cache_path.read_text())
            stats = CallStats(model=model, cache_hit=True, cost_usd=0.0)
            self._log(purpose, stats, prompt_version, file_id)
            return schema.model_validate(cached["parsed"]), stats

        if budget_usd is not None and spend_today() >= budget_usd:
            raise BudgetExceeded(f"daily LLM budget of ${budget_usd:.2f} reached")

        kwargs: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": user_input,
            "text_format": schema,
            "max_output_tokens": max_output_tokens,
            "store": False,
            "prompt_cache_key": f"{purpose}-{prompt_version or 'na'}",
        }
        if reasoning_effort:
            kwargs["reasoning"] = {"effort": reasoning_effort}

        started = time.perf_counter()
        try:
            response = self._call(**kwargs)
        except APIStatusError as exc:
            stats = CallStats(
                model=model, latency_ms=int((time.perf_counter() - started) * 1000), cost_usd=None
            )
            self._log(
                purpose, stats, prompt_version, file_id, error=f"{exc.status_code}: {exc.message}"[:500]
            )
            raise LlmError(f"OpenAI API error {exc.status_code}: {exc.message}") from exc
        latency = int((time.perf_counter() - started) * 1000)

        usage = getattr(response, "usage", None)
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        details = getattr(usage, "input_tokens_details", None)
        cached_tokens = getattr(details, "cached_tokens", 0) or 0
        stats = CallStats(
            model=model,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            output_tokens=output_tokens,
            cost_usd=estimate_cost(model, input_tokens, cached_tokens, output_tokens),
            latency_ms=latency,
        )

        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            reason = (
                getattr(getattr(response, "incomplete_details", None), "reason", None) or "no parsed output"
            )
            self._log(purpose, stats, prompt_version, file_id, error=str(reason))
            raise LlmError(f"model returned no structured output ({reason})")

        self._log(purpose, stats, prompt_version, file_id)
        cache_path.write_text(json.dumps({"model": model, "parsed": parsed.model_dump(mode="json")}))
        return parsed, stats
