"""Page-bounded, heading-aware chunks sized to the late-interaction model's 300-token document window."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from sqlalchemy import delete

from berkshire_contracts.config import get_settings
from berkshire_contracts.db import Chunk, Contract, Page, session_scope

_HEADING = re.compile(
    r"^(?:"
    r"(?i:(?:section|article|exhibit|schedule|attachment|appendix|part)\s+[\dA-Z][\w.\-]*\b.{0,80})"
    r"|\d{1,2}(?:\.\d{1,2}){0,3}\.?\s+[A-Z][A-Za-z&/,'\- ]{2,80}"
    r"|[A-Z][A-Z0-9&/,'\-. ]{4,80}"
    r")$"
)
_MAX_HEADING_WORDS = 12
_SENTENCE_SPLIT = re.compile(r"(?<=[.;:!?])\s+(?=[A-Z(\"'0-9])")

TokenCounter = Callable[[Sequence[str]], list[int]]


@dataclass
class ChunkRecord:
    chunk_id: str
    file_id: str
    page_no: int
    ordinal: int
    section: str | None
    text: str
    token_count: int


@lru_cache(maxsize=1)
def _tokenizer():
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(get_settings().colbert_model)


def token_counter() -> TokenCounter:
    tok = _tokenizer()

    def count(texts: Sequence[str]) -> list[int]:
        if not texts:
            return []
        encoded = tok(list(texts), add_special_tokens=False)["input_ids"]
        return [len(ids) for ids in encoded]

    return count


def heading_of(paragraph: str) -> str | None:
    first = paragraph.strip().split("\n", 1)[0].strip()
    if (
        4 <= len(first) <= 100
        and len(first.split()) <= _MAX_HEADING_WORDS
        and _HEADING.match(first)
        and sum(c.isalpha() for c in first) >= 4
    ):
        return first.rstrip(":").strip()
    return None


def _pack_words(text: str, max_tokens: int, count: TokenCounter) -> list[str]:
    words = text.split()
    pieces: list[str] = []
    window: list[str] = []
    used = 0
    for word, n in zip(words, count(words), strict=True):
        if window and used + n > max_tokens:
            pieces.append(" ".join(window))
            window, used = [], 0
        window.append(word)
        used += n
    if window:
        pieces.append(" ".join(window))
    return pieces


def _split_long(text: str, max_tokens: int, count: TokenCounter) -> list[str]:
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    pieces: list[str] = []
    for sentence, n in zip(sentences, count(sentences), strict=True):
        if n <= max_tokens:
            pieces.append(sentence)
        else:
            pieces.extend(_pack_words(sentence, max_tokens, count))
    return pieces


def chunk_page(
    file_id: str,
    page_no: int,
    text: str,
    section: str | None,
    count: TokenCounter,
    max_tokens: int,
    overlap_tokens: int,
    start_ordinal: int,
) -> tuple[list[ChunkRecord], str | None]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    units: list[tuple[str, str | None]] = []
    for paragraph in paragraphs:
        heading = heading_of(paragraph)
        if heading:
            section = heading[:200]
        flat = re.sub(r"\s*\n\s*", " ", paragraph)
        units.append((flat, section))

    expanded: list[tuple[str, str | None, int]] = []
    if units:
        sizes = count([u[0] for u in units])
        for (unit, sec), n in zip(units, sizes, strict=True):
            if n <= max_tokens:
                expanded.append((unit, sec, n))
            else:
                parts = _split_long(unit, max_tokens, count)
                expanded.extend((part, sec, m) for part, m in zip(parts, count(parts), strict=True))

    chunks: list[ChunkRecord] = []
    current: list[tuple[str, int]] = []
    current_section: str | None = None
    # Label each chunk with the section of its first new unit, not of the overlap carried from the last chunk.
    fresh = True
    ordinal = start_ordinal

    def flush() -> None:
        nonlocal current, ordinal, fresh
        fresh = True
        if not current:
            return
        body = " ".join(t for t, _ in current).strip()
        tokens = sum(n for _, n in current)
        chunks.append(
            ChunkRecord(
                chunk_id=f"{file_id}:{page_no}:{ordinal}",
                file_id=file_id,
                page_no=page_no,
                ordinal=ordinal,
                section=current_section,
                text=body,
                token_count=tokens,
            )
        )
        ordinal += 1
        tail: list[tuple[str, int]] = []
        carried = 0
        for unit, n in reversed(current):
            if carried + n > overlap_tokens:
                break
            tail.insert(0, (unit, n))
            carried += n
        current = tail if len(tail) < len(current) else []

    for unit, sec, n in expanded:
        if current and sum(m for _, m in current) + n > max_tokens:
            flush()
            if current and sum(m for _, m in current) + n > max_tokens:
                current = []
        if fresh:
            current_section, fresh = sec, False
        current.append((unit, n))
    if current and (not chunks or any(u not in chunks[-1].text for u, _ in current)):
        flush()
    return chunks, section


def chunk_document(pages: list[tuple[int, str]], file_id: str, count: TokenCounter) -> list[ChunkRecord]:
    settings = get_settings()
    section: str | None = None
    records: list[ChunkRecord] = []
    for page_no, text in pages:
        if not text.strip():
            continue
        page_chunks, section = chunk_page(
            file_id,
            page_no,
            text,
            section,
            count,
            settings.chunk_max_tokens,
            settings.chunk_overlap_tokens,
            start_ordinal=len(records),
        )
        records.extend(page_chunks)
    return records


def run_chunking() -> dict[str, int]:
    count = token_counter()
    stats = {"documents": 0, "chunks": 0}
    with session_scope() as s:
        targets = (
            s.query(Contract)
            .filter(Contract.duplicate_of.is_(None), Contract.parse_status.in_(["ok", "partial"]))
            .order_by(Contract.filename)
            .all()
        )
        for contract in targets:
            pages = [
                (p.page_no, p.text)
                for p in s.query(Page).filter(Page.file_id == contract.file_id).order_by(Page.page_no)
            ]
            records = chunk_document(pages, contract.file_id, count)
            s.execute(delete(Chunk).where(Chunk.file_id == contract.file_id))
            s.add_all(Chunk(**r.__dict__) for r in records)
            stats["documents"] += 1
            stats["chunks"] += len(records)
    return stats
