"""Turn sampled PDFs into page text: native text layer first, Tesseract OCR for image-only or garbled pages."""

from __future__ import annotations

import importlib.util
import re
import unicodedata
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from rich.progress import Progress
from sqlalchemy import delete

from berkshire_contracts.config import get_settings
from berkshire_contracts.db import Chunk, Contract, CorpusFile, Page, init_db, session_scope
from berkshire_contracts.ingestion.ocr import OcrResult, ocr_pdf_page
from berkshire_contracts.ingestion.pdf import PdfOpenError, is_garbled, open_pdf

PARSER_VERSION = "parse-v1"
LOW_OCR_CONFIDENCE = 55.0

_PAGE_NUMBER_LINE = re.compile(r"^\s*(page\s+)?\d+\s*(of\s+\d+)?\s*$", re.IGNORECASE)
_MULTISPACE = re.compile(r"[ \t\u00a0]{2,}")


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).replace("\r", "\n").replace("\x0c", "\n")
    lines = []
    for line in text.split("\n"):
        line = _MULTISPACE.sub(" ", line).strip()
        if _PAGE_NUMBER_LINE.match(line):
            continue
        lines.append(line)
    cleaned = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def native_blocks_text(page) -> str:
    import pymupdf

    flags = pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_DEHYPHENATE
    blocks = page.get_text("blocks", sort=True, flags=flags)
    parts = [b[4].strip() for b in blocks if len(b) > 6 and b[6] == 0 and b[4].strip()]
    return "\n\n".join(parts)


@dataclass
class ParsedPage:
    page_no: int
    text: str
    method: str
    ocr_confidence: float | None = None
    rotation: int = 0


@dataclass
class ParsedDocument:
    file_id: str
    filename: str
    pages: list[ParsedPage] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    status: str = "ok"


def _needs_ocr(text: str, min_chars: int) -> bool:
    compact = len("".join(text.split()))
    return compact < min_chars or is_garbled(text)


def summarize(doc: ParsedDocument) -> dict:
    ocr_pages = [p for p in doc.pages if p.method == "ocr"]
    confidences = [p.ocr_confidence for p in ocr_pages if p.ocr_confidence is not None]
    text_chars = sum(len(p.text) for p in doc.pages)
    methods = {p.method for p in doc.pages if p.text.strip()}
    if not methods:
        source = "none"
    elif methods == {"native"}:
        source = "native"
    elif methods == {"ocr"}:
        source = "ocr"
    else:
        source = "mixed"
    mean_conf = round(sum(confidences) / len(confidences), 1) if confidences else None
    status = doc.status
    empty_pages = sum(1 for p in doc.pages if not p.text.strip())
    if status == "ok":
        if text_chars < 40:
            status = "no_text"
        elif empty_pages or (mean_conf is not None and mean_conf < LOW_OCR_CONFIDENCE):
            status = "partial"
    if empty_pages:
        doc.notes.append(f"{empty_pages} page(s) produced no text")
    if mean_conf is not None and mean_conf < LOW_OCR_CONFIDENCE:
        doc.notes.append(f"low OCR confidence ({mean_conf})")
    return {
        "text_source": source,
        "ocr_pages": len(ocr_pages),
        "mean_ocr_confidence": mean_conf,
        "text_chars": text_chars,
        "parse_status": status,
    }


def parse_sample(force: bool = False, workers: int | None = None) -> dict[str, int]:
    settings = get_settings()
    init_db()
    tessdata = str(settings.tessdata_dir) + "/"
    if importlib.util.find_spec("tesserocr") is None:
        raise RuntimeError("OCR dependencies missing; run `uv sync --group ocr`")
    if not (settings.tessdata_dir / "eng.traineddata").exists():
        raise RuntimeError("Tesseract language data missing; run `contracts setup-ocr`")

    with session_scope() as s:
        sampled = (
            s.query(CorpusFile).filter(CorpusFile.in_sample.is_(True)).order_by(CorpusFile.filename).all()
        )
        done = {
            c.file_id
            for c in s.query(Contract).filter(
                Contract.parse_status.in_(["ok", "partial", "no_text", "failed"])
            )
        }
        sampled = [(cf.file_id, cf.filename, cf.sha256, cf.family_id, cf.page_count) for cf in sampled]

    canonical: dict[str, str] = {}
    for file_id, _filename, sha, _fam, _pc in sorted(sampled, key=lambda r: (len(r[1]), r[1])):
        canonical.setdefault(sha, file_id)

    documents: dict[str, ParsedDocument] = {}
    ocr_tasks: list[tuple[str, int, int, str]] = []
    path_to_id: dict[str, str] = {}

    with session_scope() as s:
        for file_id, filename, sha, family_id, _page_count in sampled:
            contract = s.get(Contract, file_id) or Contract(file_id=file_id, filename=filename)
            contract.family_id = family_id
            dup_of = canonical[sha] if canonical[sha] != file_id else None
            contract.duplicate_of = dup_of
            s.add(contract)
            if (file_id in done and not force) or dup_of:
                continue
            pdf_path = settings.sample_pdf_dir / filename
            doc = ParsedDocument(file_id=file_id, filename=filename)
            documents[file_id] = doc
            try:
                opened = open_pdf(pdf_path.read_bytes())
            except (PdfOpenError, OSError) as exc:
                doc.status = "failed"
                doc.notes.append(str(exc))
                continue
            doc.notes.extend(opened.notes)
            path_to_id[str(pdf_path)] = file_id
            for index, page in enumerate(opened.doc, start=1):
                try:
                    text = clean_text(native_blocks_text(page))
                except Exception as exc:
                    text = ""
                    doc.notes.append(f"page {index}: native text extraction failed ({exc})")
                if _needs_ocr(text, settings.ocr_min_native_chars):
                    doc.pages.append(ParsedPage(index, text, "pending"))
                    ocr_tasks.append((str(pdf_path), index, settings.ocr_dpi, tessdata))
                else:
                    doc.pages.append(ParsedPage(index, text, "native"))
            opened.doc.close()

    if ocr_tasks:
        results: dict[tuple[str, int], tuple[OcrResult | None, str | None]] = {}
        with Progress() as progress, ProcessPoolExecutor(max_workers=workers or settings.ocr_workers) as pool:
            bar = progress.add_task(f"OCR {len(ocr_tasks)} pages", total=len(ocr_tasks))
            futures = [pool.submit(ocr_pdf_page, task) for task in ocr_tasks]
            for future in as_completed(futures):
                path, page_no, result, error = future.result()
                results[(path, page_no)] = (result, error)
                progress.advance(bar)
        errors: dict[str, list[int]] = defaultdict(list)
        for (path, page_no), (result, error) in results.items():
            doc = documents[path_to_id[path]]
            page = doc.pages[page_no - 1]
            native = page.text
            if result is not None and len(result.text.strip()) >= len(native.strip()):
                page.text = clean_text(result.text)
                page.method = "ocr" if page.text else "empty"
                page.ocr_confidence = round(result.confidence, 1)
                page.rotation = result.rotation
            else:
                page.method = "native" if native.strip() else "empty"
                if error:
                    errors[doc.file_id].append(page_no)
        for file_id, pages in errors.items():
            documents[file_id].notes.append(f"OCR failed on page(s) {sorted(pages)[:10]}")

    counts: dict[str, int] = defaultdict(int)
    with session_scope() as s:
        for file_id, doc in documents.items():
            stats = summarize(doc)
            contract = s.get(Contract, file_id)
            assert contract is not None
            s.execute(delete(Chunk).where(Chunk.file_id == file_id))
            s.execute(delete(Page).where(Page.file_id == file_id))
            for p in doc.pages:
                s.add(
                    Page(
                        file_id=file_id,
                        page_no=p.page_no,
                        text=p.text,
                        method=p.method if p.method != "pending" else "empty",
                        ocr_confidence=p.ocr_confidence,
                        rotation=p.rotation,
                        char_count=len(p.text),
                    )
                )
            contract.page_count = len(doc.pages) or None
            for key, value in stats.items():
                setattr(contract, key, value)
            contract.parse_notes = "; ".join(dict.fromkeys(doc.notes)) or None
            counts[stats["parse_status"]] += 1

        for contract in s.query(Contract).filter(Contract.duplicate_of.is_not(None)):
            source = s.get(Contract, contract.duplicate_of)
            if source is None:
                continue
            for key in (
                "page_count",
                "text_source",
                "ocr_pages",
                "mean_ocr_confidence",
                "text_chars",
                "parse_status",
            ):
                setattr(contract, key, getattr(source, key))
            contract.parse_notes = f"exact duplicate of {source.filename}; content indexed once"
            counts["duplicate"] += 1
    return dict(counts)


def page_texts(file_id: str) -> list[tuple[int, str]]:
    with session_scope() as s:
        rows = s.query(Page).filter(Page.file_id == file_id).order_by(Page.page_no).all()
        return [(p.page_no, p.text) for p in rows]


def sample_pdf_path(filename: str) -> Path:
    return get_settings().sample_pdf_dir / filename
