"""Stage every PDF in the archive under data/pdfs."""

from __future__ import annotations

import shutil
from pathlib import Path

from berkshire_contracts.config import get_settings
from berkshire_contracts.ingestion.inventory import list_pdf_members, read_member
from berkshire_contracts.ingestion.pdf import strip_wrapper


def run_stage() -> dict[str, int]:
    """Copy every PDF out of contracts.zip, stripping the upload wrapper the way the writer always has."""
    settings = get_settings()
    settings.ensure_dirs()
    if not settings.zip_path.exists():
        raise FileNotFoundError(f"contracts archive not found at {settings.zip_path}")

    members = list_pdf_members(settings.zip_path)
    target = settings.pdf_dir
    target.mkdir(parents=True, exist_ok=True)
    keep = {Path(member).name for member in members}
    removed = 0
    for existing in target.glob("*.pdf"):
        if existing.name not in keep:
            existing.unlink()
            removed += 1

    written = 0
    for member in members:
        out = target / Path(member).name
        if out.exists():
            continue
        raw = read_member(settings.zip_path, member)
        try:
            body, _ = strip_wrapper(raw)
        except Exception:
            body = raw
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(body)
        shutil.move(tmp, out)
        written += 1
    return {"files": len(members), "written": written, "removed": removed}
