"""Retrieval benchmark: does each search mode put the labeled page near the top? No model calls."""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from berkshire_contracts.evaluation.ground_truth import load_chat_cases, load_extraction_labels
from berkshire_contracts.extraction.fields import FIELD_BY_NAME

MODES = ("plaid_rerank", "plaid", "exact", "bm25")
K = 8


@dataclass(frozen=True)
class Query:
    set_name: str
    id: str
    text: str
    expected: frozenset[tuple[str, int]]
    scope: frozenset[str] | None


def first_hit_rank(retrieved: list[tuple[str, int]], expected: frozenset[tuple[str, int]]) -> int | None:
    """1-based rank of the first retrieved (file, page) that is an expected page, or None."""
    for rank, item in enumerate(retrieved, start=1):
        if item in expected:
            return rank
    return None


def build_queries() -> list[Query]:
    queries = [
        Query("corpus-wide clause questions", c.id, c.question, frozenset(c.expected_pages), None)
        for c in load_chat_cases()
        if c.type in {"clause", "hybrid"} and c.expected_pages
    ]
    for label in load_extraction_labels():
        if label.value is None or label.page is None:
            continue
        queries.append(
            Query(
                "page within a document",
                f"{label.filename} {label.field}",
                FIELD_BY_NAME[label.field].retrieval_query,
                frozenset({(label.file_id, label.page)}),
                frozenset({label.file_id}),
            )
        )
    return queries


def run_benchmark(modes: tuple[str, ...] = MODES) -> pd.DataFrame:
    from berkshire_contracts.retrieval.index import load_index

    index = load_index()
    index.search("warm up", k=1)
    rows = []
    for q in build_queries():
        for mode in modes:
            started = time.perf_counter()
            hits = index.search(q.text, k=K, file_ids=set(q.scope) if q.scope else None, mode=mode)
            latency = (time.perf_counter() - started) * 1000
            rank = first_hit_rank([(h.chunk.file_id, h.chunk.page_no) for h in hits], q.expected)
            rows.append(
                {"set": q.set_name, "id": q.id, "mode": mode, "rank": rank, "latency_ms": round(latency, 1)}
            )
    return pd.DataFrame(rows)


def summarize(results: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for (set_name, mode), g in results.groupby(["set", "mode"], sort=False):
        ranks = g["rank"]
        rows.append(
            {
                "set": set_name,
                "mode": mode,
                "queries": len(g),
                "top_1": f"{(ranks == 1).sum()}/{len(g)}",
                f"top_{K}": f"{ranks.notna().sum()}/{len(g)}",
                "mrr": round(sum(1 / r for r in ranks.dropna()) / len(g), 3),
                "median_ms": round(statistics.median(g["latency_ms"]), 1),
            }
        )
    return pd.DataFrame(rows)


def write_results(results: pd.DataFrame, out_dir: Path) -> pd.DataFrame:
    summary = summarize(results)
    results.to_csv(out_dir / "retrieval_benchmark.csv", index=False)
    lines = [
        "# Retrieval benchmark",
        "",
        "Does each search mode return the labeled page among its top results? No model calls; "
        "rerun with `uv run python scripts/retrieval_benchmark.py`.",
        "",
        "- **Corpus-wide clause questions:** the clause and hybrid chat questions, searched across every "
        "document. A hit is any page in the answer key.",
        "- **Page within a document:** for each extraction label with a page, the field's search phrase from "
        "`fields.py`, searched only inside that document.",
        f"- `top_1` and `top_{K}`: how often the right page is first, or anywhere in the {K} passages the answer "
        "model would see. `mrr`: average of 1/rank of the first right page (1.0 is perfect).",
        "",
        summary.to_markdown(index=False),
        "",
        "The corpus-wide set has only five questions, so treat it as a sanity check; the per-document set "
        "carries most of the evidence.",
        "",
    ]
    (out_dir / "retrieval_benchmark.md").write_text("\n".join(lines))
    return summary
