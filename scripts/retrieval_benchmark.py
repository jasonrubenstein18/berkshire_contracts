"""Compare the search modes (PLAID + rerank, PLAID, exact MaxSim, BM25) on pages from the answer key.

    uv run python scripts/retrieval_benchmark.py

Writes eval/results/retrieval_benchmark.csv (one row per query and mode) and retrieval_benchmark.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from berkshire_contracts.config import get_settings  # noqa: E402
from berkshire_contracts.evaluation.retrieval import run_benchmark, write_results  # noqa: E402


def main() -> None:
    out = get_settings().eval_results_dir
    summary = write_results(run_benchmark(), out)
    print(summary.to_string(index=False))
    print(f"\nwrote {out / 'retrieval_benchmark.md'}")


if __name__ == "__main__":
    main()
