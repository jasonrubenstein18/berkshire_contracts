"""Build the zip a reviewer needs to run the app locally, without rebuilding anything.

Packs the code, tests, lockfile and README with the prebuilt SQLite database, the late-interaction index, the
staged PDFs, exports and evaluation results. Never packs .env, contracts.zip, the brief, the virtualenv, the LLM
cache or OCR scratch files, and refuses to write the zip if any packed file contains a secret set in .env.

    uv run python scripts/package_submission.py
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from berkshire_contracts.config import get_settings  # noqa: E402
from berkshire_contracts.db import snapshot_db  # noqa: E402

FILES = ("app.py", "pyproject.toml", "uv.lock", "README.md", ".env.example", ".gitignore")
CODE_DIRS = ("src/berkshire_contracts", "tests", "scripts")
DATA_DIRS = ("index", "pdfs", "exports")
EVAL_DIRS = ("ground_truth", "results")
SKIP_PARTS = {"__pycache__", ".DS_Store"}


def _tree(root: Path, prefix: str) -> dict[str, Path]:
    if not root.is_dir():
        raise SystemExit(f"missing {root}; build it with the contracts pipeline first")
    files = sorted(
        p for p in root.rglob("*") if p.is_file() and not SKIP_PARTS & set(p.relative_to(root).parts)
    )
    return {f"{prefix}/{p.relative_to(root).as_posix()}": p for p in files if p.suffix != ".pyc"}


def collect() -> dict[str, Path]:
    settings = get_settings()
    entries = {name: ROOT / name for name in FILES}
    for name in CODE_DIRS:
        entries |= _tree(ROOT / name, name)
    for name in DATA_DIRS:
        entries |= _tree(settings.data_dir / name, f"data/{name}")
    for name in EVAL_DIRS:
        entries |= _tree(settings.eval_dir / name, f"eval/{name}")
    return entries


def _is_forbidden(name: str) -> bool:
    base = Path(name).name
    env_file = base.startswith(".env") and base != ".env.example"
    return env_file or base.endswith(".zip") or base == "BP Take-Home_vShare.pdf"


def _secret_values() -> list[bytes]:
    settings = get_settings()
    secrets = (settings.openai_api_key, settings.app_password)
    values = [s.get_secret_value().strip() for s in secrets if s is not None]
    return [v.encode() for v in values if len(v) >= 8]


def _contains_secret(path: Path, secrets: list[bytes]) -> bool:
    data = path.read_bytes()
    return any(secret in data for secret in secrets)


def build(out: Path) -> Path:
    entries = collect()
    with tempfile.TemporaryDirectory() as tmp:
        entries["data/contracts.db"] = snapshot_db(Path(tmp) / "contracts.db")
        secrets = _secret_values()
        forbidden = [name for name in entries if _is_forbidden(name)]
        leaking = [name for name, path in entries.items() if secrets and _contains_secret(path, secrets)]
        if forbidden or leaking:
            raise SystemExit(
                f"refusing to package: forbidden files {forbidden}, files containing a secret {leaking}"
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, path in sorted(entries.items()):
                zf.write(path, f"{out.stem}/{name}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=ROOT / "dist" / "contract-intelligence.zip")
    args = parser.parse_args()
    out = build(args.out.resolve())
    with zipfile.ZipFile(out) as zf:
        count = len(zf.infolist())
    print(f"{out}: {count} files, {out.stat().st_size / 1e6:.0f} MB")


if __name__ == "__main__":
    main()
