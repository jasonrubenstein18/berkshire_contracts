"""Inventory every PDF in the archive without extracting it to disk."""

from __future__ import annotations

import hashlib
import zipfile
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd
from rich.progress import track
from sqlalchemy import delete

from berkshire_contracts.config import get_settings
from berkshire_contracts.db import CorpusFile, init_db, session_scope
from berkshire_contracts.ingestion.filenames import file_id_for, parse_filename
from berkshire_contracts.ingestion.pdf import PdfOpenError, find_pdf_offset, native_page_text, open_pdf

LOW_TEXT_CHARS = 80


def list_pdf_members(zip_path: Path) -> list[str]:
    with zipfile.ZipFile(zip_path) as zf:
        return sorted(
            info.filename
            for info in zf.infolist()
            if not info.is_dir()
            and info.filename.lower().endswith(".pdf")
            and not info.filename.startswith("__MACOSX/")
            and not Path(info.filename).name.startswith("._")
        )


def read_member(zip_path: Path, member: str) -> bytes:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.read(member)


def text_profile(page_count: int, low_text_pages: int) -> str:
    if page_count == 0:
        return "unreadable"
    share = low_text_pages / page_count
    if share >= 0.8:
        return "scanned"
    if share > 0.2:
        return "mixed"
    return "native"


def profile_member(zip_path: str, member: str) -> dict[str, Any]:
    raw = read_member(Path(zip_path), member)
    filename = Path(member).name
    facts = parse_filename(filename)
    record: dict[str, Any] = {
        "file_id": file_id_for(member),
        "filename": filename,
        "zip_member": member,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "pdf_offset": max(find_pdf_offset(raw), 0),
        "page_count": None,
        "native_chars": None,
        "low_text_pages": None,
        "text_profile": "unreadable",
        "encrypted": False,
        "open_error": None,
        "open_notes": None,
        "heuristic_doc_type": facts.heuristic_doc_type,
        "family_id": facts.family_id,
        "sub_contract_id": facts.sub_contract_id,
        "related_contracts": facts.related_contracts,
    }
    try:
        opened = open_pdf(raw)
    except PdfOpenError as exc:
        record["open_error"] = str(exc)
        return record

    doc = opened.doc
    total_chars = 0
    low_pages = 0
    for page in doc:
        try:
            chars = len("".join(native_page_text(page).split()))
        except Exception:
            chars = 0
        total_chars += chars
        low_pages += chars < LOW_TEXT_CHARS
    record.update(
        page_count=doc.page_count,
        native_chars=total_chars,
        low_text_pages=low_pages,
        text_profile=text_profile(doc.page_count, low_pages),
        encrypted=opened.encrypted,
        open_notes="; ".join(opened.notes) or None,
    )
    doc.close()
    return record


def assign_duplicate_groups(records: list[dict[str, Any]]) -> None:
    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        by_hash[rec["sha256"]].append(rec)
    for sha, group in by_hash.items():
        if len(group) > 1:
            for rec in group:
                rec["duplicate_group"] = sha[:12]
        else:
            group[0]["duplicate_group"] = None


def run_inventory(workers: int | None = None) -> pd.DataFrame:
    settings = get_settings()
    settings.ensure_dirs()
    if not settings.zip_path.exists():
        raise FileNotFoundError(f"contracts archive not found at {settings.zip_path}")
    members = list_pdf_members(settings.zip_path)
    records: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=workers or settings.ocr_workers) as pool:
        futures = [pool.submit(profile_member, str(settings.zip_path), m) for m in members]
        for future in track(futures, description="Profiling PDFs"):
            records.append(future.result())
    assign_duplicate_groups(records)

    init_db()
    with session_scope() as s:
        existing = {cf.file_id: cf for cf in s.query(CorpusFile).all()}
        keep_sample = {
            fid: (cf.in_sample, cf.sample_stratum, cf.sample_reason) for fid, cf in existing.items()
        }
        s.execute(delete(CorpusFile))
        for rec in records:
            in_sample, stratum, reason = keep_sample.get(rec["file_id"], (False, None, None))
            s.add(CorpusFile(**rec, in_sample=in_sample, sample_stratum=stratum, sample_reason=reason))

    frame = pd.DataFrame(records).sort_values("filename")
    frame.to_csv(settings.processed_dir / "inventory.csv", index=False)
    return frame
