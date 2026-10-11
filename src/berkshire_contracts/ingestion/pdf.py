"""Defensive PDF opening: upload-wrapper stripping, empty-password decryption, and pikepdf repair."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pymupdf

pymupdf.TOOLS.mupdf_display_errors(False)
pymupdf.TOOLS.mupdf_display_warnings(False)

PDF_MAGIC = b"%PDF"
_HEADER_SCAN_BYTES = 4096
_GARBAGE_RE = re.compile(r"\(cid:\d+\)|\ufffd")


class PdfOpenError(RuntimeError):
    """Raised when a file cannot be opened as a PDF even after repair attempts."""


@dataclass
class OpenedPdf:
    doc: pymupdf.Document
    data: bytes
    notes: list[str] = field(default_factory=list)
    encrypted: bool = False
    repaired: bool = False


def find_pdf_offset(data: bytes) -> int:
    """Byte offset of the `%PDF` header, or -1. Non-zero offsets indicate an upload wrapper."""
    return data.find(PDF_MAGIC, 0, _HEADER_SCAN_BYTES)


def strip_wrapper(data: bytes) -> tuple[bytes, int]:
    offset = find_pdf_offset(data)
    if offset < 0:
        raise PdfOpenError("no %PDF header in the first 4 KB; not a PDF")
    if offset == 0:
        return data, 0
    body = data[offset:]
    eof = body.rfind(b"%%EOF")
    if eof > 0:
        body = body[: eof + len(b"%%EOF")] + b"\n"
    return body, offset


def _repair_with_pikepdf(data: bytes) -> bytes:
    import pikepdf

    with pikepdf.open(io.BytesIO(data), suppress_warnings=True, attempt_recovery=True) as pdf:
        out = io.BytesIO()
        pdf.save(out)
        return out.getvalue()


def open_pdf(raw: bytes) -> OpenedPdf:
    """Open PDF bytes, recording every recovery step in `notes` instead of failing silently."""
    data, offset = strip_wrapper(raw)
    notes: list[str] = []
    if offset:
        notes.append(f"stripped {offset}-byte upload wrapper before %PDF header")

    pymupdf.TOOLS.reset_mupdf_warnings()
    repaired = False
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
        if doc.page_count == 0:
            raise PdfOpenError("PDF has no pages")
    except Exception as first_error:
        notes.append(f"PyMuPDF could not open file ({first_error}); attempted pikepdf repair")
        try:
            data = _repair_with_pikepdf(data)
            doc = pymupdf.open(stream=data, filetype="pdf")
            repaired = True
        except Exception as second_error:
            raise PdfOpenError(f"unreadable PDF: {first_error}; repair failed: {second_error}") from None

    encrypted = bool(doc.is_encrypted or doc.metadata.get("encryption"))
    if doc.needs_pass:
        if not doc.authenticate(""):
            raise PdfOpenError("encrypted with a non-empty user password")
        notes.append("encrypted; opened with empty user password")
        encrypted = True
    elif encrypted:
        notes.append("encrypted (owner-password permissions only)")

    warnings = pymupdf.TOOLS.mupdf_warnings()
    if warnings:
        repaired = True
        first_lines = "; ".join(sorted(set(warnings.splitlines()))[:3])
        notes.append(f"MuPDF repaired structural errors: {first_lines[:240]}")

    if doc.page_count == 0:
        raise PdfOpenError("PDF has no pages")
    return OpenedPdf(doc=doc, data=data, notes=notes, encrypted=encrypted, repaired=repaired)


def native_page_text(page: pymupdf.Page) -> str:
    flags = pymupdf.TEXTFLAGS_TEXT | pymupdf.TEXT_DEHYPHENATE
    return page.get_text("text", sort=True, flags=flags)


def text_quality(text: str) -> tuple[int, float]:
    """Return (non-space character count, share of characters that are letters or digits)."""
    compact = "".join(text.split())
    if not compact:
        return 0, 0.0
    alnum = sum(ch.isalnum() for ch in compact)
    return len(compact), alnum / len(compact)


def is_garbled(text: str) -> bool:
    chars, alnum_share = text_quality(text)
    if chars == 0:
        return False
    return alnum_share < 0.55 or len(_GARBAGE_RE.findall(text)) > max(5, chars // 200)
