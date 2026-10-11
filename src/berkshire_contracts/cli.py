"""`contracts` command-line entry point. Every step is resumable and records a row in `pipeline_runs`."""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from collections.abc import Callable
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from berkshire_contracts.config import get_settings

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Contract intelligence pipeline.")
console = Console()

TESSDATA_URLS = {
    "eng.traineddata": "https://github.com/tesseract-ocr/tessdata_best/raw/main/eng.traineddata",
    "osd.traineddata": "https://github.com/tesseract-ocr/tessdata/raw/main/osd.traineddata",
}


def _run_step(name: str, fn: Callable[[], Any]) -> Any:
    from berkshire_contracts.db import finish_run, init_db, start_run

    init_db()
    run_id = start_run(name)
    started = time.perf_counter()
    try:
        result = fn()
    except Exception as exc:
        finish_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}")
        raise
    stats = (
        result if isinstance(result, dict) else {"rows": len(result)} if hasattr(result, "__len__") else {}
    )
    stats = {**stats, "seconds": round(time.perf_counter() - started, 1)}
    finish_run(run_id, "succeeded", stats=json.loads(json.dumps(stats, default=str)))
    console.print(f"[green]{name}[/green] done: {stats}")
    return result


@app.callback()
def _main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )


@app.command("setup-ocr")
def setup_ocr() -> None:
    """Download Tesseract language data (English LSTM model and orientation detection)."""
    settings = get_settings()
    settings.tessdata_dir.mkdir(parents=True, exist_ok=True)
    for name, url in TESSDATA_URLS.items():
        target = settings.tessdata_dir / name
        if target.exists():
            console.print(f"{name} already present")
            continue
        console.print(f"downloading {name}")
        urllib.request.urlretrieve(url, target)


@app.command()
def inventory() -> None:
    """Profile every PDF in contracts.zip (duplicates, encryption, text layer, contract family)."""
    from berkshire_contracts.ingestion.inventory import run_inventory

    frame = _run_step("inventory", run_inventory)
    table = Table("text profile", "files", "pages")
    for profile, group in frame.groupby("text_profile"):
        table.add_row(profile, str(len(group)), str(int(group.page_count.fillna(0).sum())))
    console.print(table)


@app.command()
def stage() -> None:
    """Copy every PDF out of contracts.zip into data/pdfs, stripping upload wrappers."""
    from berkshire_contracts.ingestion.staging import run_stage

    _run_step("stage", run_stage)


@app.command()
def parse(force: bool = typer.Option(False, help="Re-parse documents that already have pages.")) -> None:
    """Extract page text with PyMuPDF and OCR image-only pages with Tesseract."""
    from berkshire_contracts.ingestion.parsing import parse_documents

    _run_step("parse", lambda: parse_documents(force=force))


@app.command()
def chunk() -> None:
    """Split page text into page-bounded, heading-aware chunks."""
    from berkshire_contracts.ingestion.chunking import run_chunking

    _run_step("chunk", run_chunking)


@app.command()
def extract(
    limit: int | None = typer.Option(None, help="Only process the first N pending documents."),
    file_id: list[str] = typer.Option(None, "--file-id", help="Restrict to specific file ids (repeatable)."),
    force: bool = typer.Option(False, help="Re-extract documents that already have results."),
    escalate: bool = typer.Option(True, help="Re-read flagged documents with more reasoning effort."),
) -> None:
    """Extract the 19 fields with evidence, verify quotes, escalate flagged documents, write to SQLite."""
    from berkshire_contracts.extraction.extractor import run_extraction

    _run_step(
        "extract",
        lambda: run_extraction(file_ids=file_id or None, force=force, limit=limit, escalate=escalate),
    )


@app.command()
def index() -> None:
    """Encode chunks with the late-interaction model and build the PLAID, exact-MaxSim and BM25 indexes."""
    from berkshire_contracts.retrieval.index import build_index

    _run_step("index", build_index)


@app.command()
def export() -> None:
    """Write contracts.csv (one row per file), field_evidence.csv and contracts.xlsx to data/exports."""
    from berkshire_contracts.exports import write_exports

    _run_step("export", write_exports)


@app.command()
def evaluate(
    judge: bool = typer.Option(True, help="Use the LLM judge for free-text fields and chat answers."),
) -> None:
    """Score extraction, retrieval (late interaction vs BM25) and chat against eval/ground_truth."""
    from berkshire_contracts.evaluation.runner import run_evaluation

    _run_step("evaluate", lambda: run_evaluation(use_judge=judge))


@app.command()
def ask(question: str) -> None:
    """Ask the assistant a question from the command line."""
    from berkshire_contracts.chat.service import ContractAssistant

    response = ContractAssistant().ask(question, session="cli")
    console.print(
        f"[bold]route:[/bold] {response.route}  [dim]{response.latency_ms} ms, ${response.cost_usd:.4f}[/dim]"
    )
    console.print(response.answer)
    if response.table is not None:
        console.print(response.table.head(20).to_string())
    for src in response.sources:
        console.print(f"[dim]{src.sid} {src.filename} p.{src.page} score={src.score:.3f}[/dim]")
    for note in response.notes:
        console.print(f"[yellow]note:[/yellow] {note}")


@app.command()
def status() -> None:
    """Summarize what is in the database."""
    from sqlalchemy import func

    from berkshire_contracts.db import Chunk, Contract, CorpusFile, LlmCall, Page, init_db, session_scope

    init_db()
    with session_scope() as s:
        table = Table("metric", "value")
        table.add_row("corpus files", str(s.query(CorpusFile).count()))
        table.add_row("contract rows", str(s.query(Contract).count()))
        for status_value, n in s.query(Contract.parse_status, func.count()).group_by(Contract.parse_status):
            table.add_row(f"parse: {status_value}", str(n))
        for status_value, n in s.query(Contract.extraction_status, func.count()).group_by(
            Contract.extraction_status
        ):
            table.add_row(f"extraction: {status_value}", str(n))
        table.add_row("pages", str(s.query(Page).count()))
        table.add_row("OCR pages", str(s.query(Page).filter(Page.method == "ocr").count()))
        table.add_row("chunks", str(s.query(Chunk).count()))
        cost = s.query(func.sum(LlmCall.cost_usd)).scalar() or 0.0
        table.add_row("LLM spend (USD)", f"{cost:.4f}")
    console.print(table)


if __name__ == "__main__":
    app()
