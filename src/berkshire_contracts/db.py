"""SQLite schema. `contracts` is the deliverable table: one row per contract file in the processed sample."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from berkshire_contracts.config import get_settings


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[str]: JSON}


class CorpusFile(Base):
    """Every PDF in the archive (the sampling frame), with cheap metadata gathered during inventory."""

    __tablename__ = "corpus_files"

    file_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    zip_member: Mapped[str] = mapped_column(String(320))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    pdf_offset: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int | None] = mapped_column(Integer)
    native_chars: Mapped[int | None] = mapped_column(Integer)
    low_text_pages: Mapped[int | None] = mapped_column(Integer)
    text_profile: Mapped[str] = mapped_column(String(16))
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    open_error: Mapped[str | None] = mapped_column(Text)
    open_notes: Mapped[str | None] = mapped_column(Text)
    heuristic_doc_type: Mapped[str] = mapped_column(String(32))
    family_id: Mapped[str | None] = mapped_column(String(16), index=True)
    sub_contract_id: Mapped[str | None] = mapped_column(String(24))
    related_contracts: Mapped[list[str]] = mapped_column(JSON, default=list)
    duplicate_group: Mapped[str | None] = mapped_column(String(16))
    in_sample: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    sample_stratum: Mapped[str | None] = mapped_column(String(64))
    sample_reason: Mapped[str | None] = mapped_column(String(64))


class Contract(Base):
    """One row per processed contract file: parse quality, the 19 extracted fields, and review flags."""

    __tablename__ = "contracts"

    file_id: Mapped[str] = mapped_column(ForeignKey("corpus_files.file_id"), primary_key=True)
    filename: Mapped[str] = mapped_column(String(255), unique=True)
    family_id: Mapped[str | None] = mapped_column(String(16), index=True)
    duplicate_of: Mapped[str | None] = mapped_column(String(16))

    page_count: Mapped[int | None] = mapped_column(Integer)
    text_source: Mapped[str | None] = mapped_column(String(16))
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    mean_ocr_confidence: Mapped[float | None] = mapped_column(Float)
    text_chars: Mapped[int] = mapped_column(Integer, default=0)
    parse_status: Mapped[str] = mapped_column(String(16), default="pending")
    parse_notes: Mapped[str | None] = mapped_column(Text)

    contract_number: Mapped[str | None] = mapped_column(String(64))
    document_type: Mapped[str | None] = mapped_column(String(32), index=True)
    vendor_name: Mapped[str | None] = mapped_column(String(255), index=True)
    contract_description: Mapped[str | None] = mapped_column(Text)
    effective_date: Mapped[date | None] = mapped_column(Date)
    expiration_date: Mapped[date | None] = mapped_column(Date, index=True)
    renewal_type: Mapped[str | None] = mapped_column(String(32))
    renewal_options: Mapped[str | None] = mapped_column(Text)
    auto_renewal: Mapped[bool | None] = mapped_column(Boolean)
    renewal_notice_days: Mapped[int | None] = mapped_column(Integer)
    award_amount_usd: Mapped[float | None] = mapped_column(Float)
    pricing_basis: Mapped[str | None] = mapped_column(String(32))
    termination_for_convenience: Mapped[bool | None] = mapped_column(Boolean)
    termination_notice_days: Mapped[int | None] = mapped_column(Integer)
    liability_cap: Mapped[str | None] = mapped_column(Text)
    indemnification: Mapped[str | None] = mapped_column(String(48))
    insurance_requirements: Mapped[str | None] = mapped_column(Text)
    governing_law: Mapped[str | None] = mapped_column(String(64))
    assignment_change_of_control: Mapped[str | None] = mapped_column(String(48))

    extraction_status: Mapped[str] = mapped_column(String(24), default="pending")
    fields_found: Mapped[int] = mapped_column(Integer, default=0)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_reasons: Mapped[str | None] = mapped_column(Text)
    extraction_model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime)


class FieldEvidence(Base):
    """Field-level provenance: the value, the verbatim supporting quote, its page, and verification result."""

    __tablename__ = "field_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("contracts.file_id"), index=True)
    field_name: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(16))
    value_text: Mapped[str | None] = mapped_column(Text)
    quote: Mapped[str | None] = mapped_column(Text)
    page: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[str | None] = mapped_column(String(8))
    verified: Mapped[bool | None] = mapped_column(Boolean)
    match_score: Mapped[float | None] = mapped_column(Float)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))


class Page(Base):
    __tablename__ = "pages"

    file_id: Mapped[str] = mapped_column(ForeignKey("contracts.file_id"), primary_key=True)
    page_no: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")
    method: Mapped[str] = mapped_column(String(12))
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    rotation: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)


class Chunk(Base):
    __tablename__ = "chunks"

    chunk_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("contracts.file_id"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    ordinal: Mapped[int] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)


class LlmCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    purpose: Mapped[str] = mapped_column(String(24), index=True)
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    file_id: Mapped[str | None] = mapped_column(String(16))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)


class ChatLog(Base):
    __tablename__ = "chat_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    session: Mapped[str | None] = mapped_column(String(64))
    question: Mapped[str] = mapped_column(Text)
    route: Mapped[str | None] = mapped_column(String(24))
    plan: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    retrieved: Mapped[list[str] | None] = mapped_column(JSON)
    answer: Mapped[str | None] = mapped_column(Text)
    insufficient_evidence: Mapped[bool | None] = mapped_column(Boolean)
    top_score: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    step: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default="running")
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)


FAMILY_VIEW_SQL = """
CREATE VIEW IF NOT EXISTS contract_families AS
SELECT
    family_id,
    COUNT(*) AS files,
    GROUP_CONCAT(DISTINCT document_type) AS document_types,
    GROUP_CONCAT(DISTINCT vendor_name) AS vendors,
    MIN(effective_date) AS earliest_effective_date,
    MAX(expiration_date) AS latest_expiration_date,
    MAX(award_amount_usd) AS max_award_amount_usd,
    MAX(CASE WHEN auto_renewal = 1 THEN 1 ELSE 0 END) AS any_auto_renewal,
    MAX(CASE WHEN termination_for_convenience = 1 THEN 1 ELSE 0 END) AS any_termination_for_convenience,
    SUM(CASE WHEN needs_review = 1 THEN 1 ELSE 0 END) AS files_needing_review
FROM contracts
WHERE family_id IS NOT NULL AND duplicate_of IS NULL
GROUP BY family_id
"""


def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


@lru_cache(maxsize=4)
def get_engine(db_path: Path | None = None) -> Engine:
    path = db_path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}", future=True, connect_args={"check_same_thread": False, "timeout": 30}
    )
    event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def init_db(db_path: Path | None = None) -> Engine:
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(FAMILY_VIEW_SQL))
    return engine


def snapshot_db(dest: Path, db_path: Path | None = None) -> Path:
    """Copy the live WAL-mode database into one self-contained file, consistent even while the app runs."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    source_path = db_path or get_settings().db_path
    with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(dest)) as target:
        source.backup(target)
        target.execute("PRAGMA journal_mode=DELETE")
    return dest


@lru_cache(maxsize=4)
def _session_factory(db_path: Path | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(db_path), expire_on_commit=False, future=True)


@contextmanager
def session_scope(db_path: Path | None = None) -> Iterator[Session]:
    session = _session_factory(db_path)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def start_run(step: str) -> int:
    with session_scope() as s:
        run = PipelineRun(step=step)
        s.add(run)
        s.flush()
        return run.id


def finish_run(
    run_id: int, status: str, stats: dict[str, Any] | None = None, error: str | None = None
) -> None:
    with session_scope() as s:
        run = s.get(PipelineRun, run_id)
        if run is None:
            return
        run.finished_at = utcnow()
        run.status = status
        run.stats = stats
        run.error = error
