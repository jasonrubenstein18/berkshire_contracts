"""Evidence-first extraction of the 19 fields, with quote verification, consistency checks and bounded escalation."""

from __future__ import annotations

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from rich.progress import Progress
from sqlalchemy import delete

from berkshire_contracts.config import get_settings
from berkshire_contracts.db import Chunk, Contract, FieldEvidence, Page, session_scope, utcnow
from berkshire_contracts.extraction.fields import FIELD_BY_NAME, FIELD_SPECS
from berkshire_contracts.extraction.llm import LlmClient
from berkshire_contracts.extraction.normalize import (
    clean_amount,
    clean_days,
    clean_text_value,
    family_from_contract_number,
    norm_text,
    normalize_contract_number,
    parse_iso_date,
    verify_quote,
)
from berkshire_contracts.extraction.prompts import (
    ESCALATION_INSTRUCTIONS,
    ESCALATION_PROMPT_VERSION,
    EXTRACTION_INSTRUCTIONS,
    EXTRACTION_PROMPT_VERSION,
    document_block,
    escalation_request,
)
from berkshire_contracts.schemas import EXTRACTION_FIELDS, ContractExtraction

log = logging.getLogger(__name__)

LONG_DOC_HEAD_PAGES = 4
LONG_DOC_TAIL_PAGES = 2
LONG_DOC_CHUNKS_PER_FIELD = 3
LONG_DOC_CHAR_BUDGET = 110_000


@dataclass
class DocumentContext:
    file_id: str
    filename: str
    page_count: int
    pages: dict[int, str]
    context_pages: list[int]
    partial: bool
    normalized: dict[int, str] = field(default_factory=dict)

    def block(self) -> str:
        return document_block(
            self.filename, self.page_count, [(p, self.pages[p]) for p in self.context_pages], self.partial
        )


@dataclass
class FieldResult:
    name: str
    status: str
    value: Any
    value_text: str | None
    quote: str | None
    page: int | None
    confidence: str
    verified: bool | None
    match_score: float | None
    escalated: bool = False
    problem: str | None = None


@dataclass
class DocumentResult:
    file_id: str
    fields: dict[str, FieldResult]
    model: str
    prompt_version: str
    consistency_issues: list[str] = field(default_factory=list)
    escalated: bool = False
    error: str | None = None


def select_long_document_pages(file_id: str, pages: dict[int, str]) -> list[int]:
    """First/last pages plus the pages whose chunks best match each field's retrieval query (exact MaxSim)."""
    from berkshire_contracts.retrieval.colbert import get_encoder, maxsim_many

    ordered = sorted(pages)
    chosen: list[int] = ordered[:LONG_DOC_HEAD_PAGES] + ordered[-LONG_DOC_TAIL_PAGES:]
    with session_scope() as s:
        chunks = s.query(Chunk).filter(Chunk.file_id == file_id).order_by(Chunk.page_no, Chunk.ordinal).all()
        chunk_rows = [(c.page_no, c.text) for c in chunks]
    if chunk_rows:
        encoder = get_encoder()
        embeddings = encoder.encode_documents([t for _, t in chunk_rows], batch_size=32)
        ranked_pages: list[int] = []
        for spec in FIELD_SPECS:
            scores = maxsim_many(encoder.encode_query(spec.retrieval_query), embeddings)
            for idx in scores.argsort()[::-1][:LONG_DOC_CHUNKS_PER_FIELD]:
                ranked_pages.append(chunk_rows[int(idx)][0])
        for page in ranked_pages:
            if page not in chosen:
                chosen.append(page)
    selected: list[int] = []
    used = 0
    for page in chosen:
        size = len(pages.get(page, ""))
        if selected and used + size > LONG_DOC_CHAR_BUDGET:
            continue
        selected.append(page)
        used += size
    return sorted(set(selected))


def build_context(file_id: str, filename: str) -> DocumentContext:
    settings = get_settings()
    with session_scope() as s:
        rows = s.query(Page).filter(Page.file_id == file_id).order_by(Page.page_no).all()
        pages = {p.page_no: p.text for p in rows if p.text.strip()}
        page_count = len(rows)
    total = sum(len(t) for t in pages.values())
    if total <= settings.full_context_max_chars:
        context_pages, partial = sorted(pages), False
    else:
        context_pages, partial = select_long_document_pages(file_id, pages), True
    ctx = DocumentContext(file_id, filename, page_count, pages, context_pages, partial)
    ctx.normalized = {p: norm_text(t) for p, t in pages.items()}
    return ctx


def _normalize_value(kind: str, name: str, raw: Any) -> tuple[Any, str | None, str | None]:
    """Returns (typed value, display text, problem)."""
    if raw is None:
        return None, None, None
    if kind == "date":
        parsed = parse_iso_date(str(raw))
        return (parsed, parsed.isoformat(), None) if parsed else (None, None, f"invalid date {raw!r}")
    if kind == "amount":
        amount = clean_amount(raw)
        return (
            (amount, f"{amount:.2f}", None)
            if amount is not None
            else (None, None, f"implausible amount {raw!r}")
        )
    if kind == "days":
        days = clean_days(raw)
        return (days, str(days), None) if days is not None else (None, None, f"implausible days {raw!r}")
    if kind == "bool":
        return bool(raw), "true" if raw else "false", None
    if name == "contract_number":
        number = normalize_contract_number(str(raw))
        return number, number, None
    text = clean_text_value(str(raw))
    return text, text, None


def postprocess(
    extraction: ContractExtraction, ctx: DocumentContext, model: str, version: str
) -> DocumentResult:
    results: dict[str, FieldResult] = {}
    for name in EXTRACTION_FIELDS:
        spec = FIELD_BY_NAME[name]
        item = getattr(extraction, name)
        found = item.status == "found" and item.value is not None
        if not found:
            results[name] = FieldResult(
                name, "not_found", None, None, None, None, item.confidence, None, None
            )
            continue
        value, display, problem = _normalize_value(spec.kind, name, item.value)
        if value is None:
            results[name] = FieldResult(
                name,
                "not_found",
                None,
                None,
                item.evidence_quote,
                item.evidence_page,
                "low",
                False,
                None,
                problem=problem or "value empty after normalization",
            )
            continue
        check = verify_quote(item.evidence_quote, ctx.normalized, item.evidence_page)
        results[name] = FieldResult(
            name,
            "found",
            value,
            display,
            item.evidence_quote,
            check.page,
            item.confidence,
            check.verified,
            check.score,
            problem=None if check.verified else "evidence quote not found in document",
        )
    doc = DocumentResult(ctx.file_id, results, model, version)
    doc.consistency_issues = consistency_issues(results)
    return doc


def consistency_issues(fields: dict[str, FieldResult]) -> list[str]:
    issues: list[str] = []
    eff, exp = fields["effective_date"].value, fields["expiration_date"].value
    if isinstance(eff, date) and isinstance(exp, date) and exp < eff:
        issues.append("expiration_date precedes effective_date")
    auto, rtype = fields["auto_renewal"], fields["renewal_type"]
    if auto.value is True and rtype.value not in (None, "automatic"):
        issues.append(f"auto_renewal is true but renewal_type is {rtype.value}")
    if rtype.value == "automatic" and auto.value is False:
        issues.append("renewal_type is automatic but auto_renewal is false")
    return issues


def flags_for(doc: DocumentResult) -> dict[str, str]:
    flags: dict[str, str] = {}
    for name, f in doc.fields.items():
        if f.problem:
            flags[name] = f.problem
        elif f.status == "found" and f.confidence == "low":
            flags[name] = "low confidence"
    for issue in doc.consistency_issues:
        for name in ("effective_date", "expiration_date", "auto_renewal", "renewal_type"):
            if name in issue:
                flags.setdefault(name, issue)
    return flags


def merge_escalation(
    first: DocumentResult, second: DocumentResult, flagged: dict[str, str]
) -> DocumentResult:
    merged = dict(first.fields)
    for name in flagged:
        new = second.fields[name]
        old = first.fields[name]
        better = (
            (new.status == "found" and new.verified)
            or (new.status == "not_found" and old.problem is not None)
            or (old.status == "found" and not old.verified)
        )
        if better:
            new.escalated = True
            merged[name] = new
    doc = DocumentResult(first.file_id, merged, second.model, second.prompt_version, escalated=True)
    doc.consistency_issues = consistency_issues(merged)
    return doc


def review_reasons(doc: DocumentResult, contract: Contract) -> list[str]:
    reasons: list[str] = []
    unverified = [n for n, f in doc.fields.items() if f.status == "found" and not f.verified]
    if unverified:
        reasons.append("unverified evidence: " + ", ".join(unverified))
    low = [n for n, f in doc.fields.items() if f.status == "found" and f.confidence == "low"]
    if low:
        reasons.append("low confidence: " + ", ".join(low))
    reasons.extend(doc.consistency_issues)
    stated_family = family_from_contract_number(doc.fields["contract_number"].value)
    if stated_family and contract.family_id and stated_family != contract.family_id:
        reasons.append(
            f"contract number in document ({doc.fields['contract_number'].value}) differs from filename "
            f"({contract.family_id})"
        )
    if contract.parse_status == "partial":
        reasons.append("some pages had no extractable text")
    if contract.mean_ocr_confidence is not None and contract.mean_ocr_confidence < 70:
        reasons.append(f"low OCR confidence ({contract.mean_ocr_confidence})")
    return reasons


def _extract(
    client: LlmClient, ctx: DocumentContext, escalate_flags: dict[str, str] | None = None
) -> DocumentResult:
    settings = get_settings()
    if escalate_flags:
        model, effort = settings.escalation_model, settings.escalation_reasoning_effort
        instructions, version = ESCALATION_INSTRUCTIONS, ESCALATION_PROMPT_VERSION
        user_input = escalation_request(ctx.block(), escalate_flags)
        purpose = "escalate"
    else:
        model, effort = settings.extraction_model, settings.extraction_reasoning_effort
        instructions, version = EXTRACTION_INSTRUCTIONS, EXTRACTION_PROMPT_VERSION
        user_input = ctx.block()
        purpose = "extract"
    parsed, _ = client.parse(
        purpose=purpose,
        model=model,
        instructions=instructions,
        user_input=user_input,
        schema=ContractExtraction,
        prompt_version=version,
        file_id=ctx.file_id,
        reasoning_effort=effort,
        max_output_tokens=24000,
    )
    return postprocess(parsed, ctx, model, version)


def _write(doc: DocumentResult, contract: Contract, session) -> None:
    session.execute(delete(FieldEvidence).where(FieldEvidence.file_id == contract.file_id))
    for name, f in doc.fields.items():
        setattr(contract, name, f.value)
        session.add(
            FieldEvidence(
                file_id=contract.file_id,
                field_name=name,
                status=f.status,
                value_text=f.value_text,
                quote=f.quote,
                page=f.page,
                confidence=f.confidence,
                verified=f.verified,
                match_score=f.match_score,
                escalated=f.escalated,
                model=doc.model,
                prompt_version=doc.prompt_version,
            )
        )
    reasons = review_reasons(doc, contract)
    contract.fields_found = sum(1 for f in doc.fields.values() if f.status == "found")
    contract.needs_review = bool(reasons)
    contract.review_reasons = "; ".join(reasons) or None
    contract.extraction_status = "escalated" if doc.escalated else "ok"
    contract.extraction_model = doc.model
    contract.prompt_version = doc.prompt_version
    contract.extracted_at = utcnow()
    family = family_from_contract_number(contract.contract_number)
    if contract.family_id is None and family:
        contract.family_id = family


def _mark_skipped(session) -> int:
    skipped = 0
    for contract in session.query(Contract).filter(
        Contract.duplicate_of.is_(None), Contract.parse_status.in_(["failed", "no_text"])
    ):
        contract.extraction_status = (
            "skipped_unreadable" if contract.parse_status == "failed" else "skipped_no_text"
        )
        contract.needs_review = True
        contract.review_reasons = (
            "file could not be opened; manual review required"
            if contract.parse_status == "failed"
            else "no extractable text even after OCR; manual review required"
        )
        skipped += 1
    return skipped


def _copy_duplicates(session) -> int:
    copied = 0
    for contract in session.query(Contract).filter(Contract.duplicate_of.is_not(None)):
        source = session.get(Contract, contract.duplicate_of)
        if source is None:
            continue
        for name in (
            *EXTRACTION_FIELDS,
            "fields_found",
            "needs_review",
            "review_reasons",
            "extraction_model",
            "prompt_version",
            "extracted_at",
        ):
            setattr(contract, name, getattr(source, name))
        contract.family_id = contract.family_id or source.family_id
        contract.extraction_status = "duplicate"
        session.execute(delete(FieldEvidence).where(FieldEvidence.file_id == contract.file_id))
        for ev in session.query(FieldEvidence).filter(FieldEvidence.file_id == source.file_id).all():
            session.add(
                FieldEvidence(
                    **{c.name: getattr(ev, c.name) for c in FieldEvidence.__table__.columns if c.name != "id"}
                    | {"file_id": contract.file_id}
                )
            )
        copied += 1
    return copied


def run_extraction(
    file_ids: list[str] | None = None, force: bool = False, limit: int | None = None, escalate: bool = True
) -> dict[str, Any]:
    settings = get_settings()
    client = LlmClient(settings)
    with session_scope() as s:
        q = s.query(Contract).filter(
            Contract.duplicate_of.is_(None), Contract.parse_status.in_(["ok", "partial"])
        )
        if file_ids:
            q = q.filter(Contract.file_id.in_(file_ids))
        elif not force:
            q = q.filter(Contract.extraction_status.in_(["pending", "failed"]))
        targets = [(c.file_id, c.filename) for c in q.order_by(Contract.filename).all()]
    if limit:
        targets = targets[:limit]

    contexts = {fid: build_context(fid, name) for fid, name in targets}
    final: dict[str, DocumentResult] = {}
    failed: dict[str, str] = {}

    def persist(fid: str, doc: DocumentResult | None, error: str | None = None) -> None:
        with session_scope() as s:
            contract = s.get(Contract, fid)
            assert contract is not None
            if doc is not None:
                _write(doc, contract, s)
            else:
                contract.extraction_status = "failed"
                contract.needs_review = True
                contract.review_reasons = f"extraction failed: {(error or '')[:300]}"

    def run_pass(jobs: dict[str, dict[str, str] | None], label: str, on_result) -> None:
        with Progress() as progress, ThreadPoolExecutor(max_workers=settings.extraction_concurrency) as pool:
            bar = progress.add_task(label, total=len(jobs))
            futures = {
                pool.submit(_extract, client, contexts[fid], flags): fid for fid, flags in jobs.items()
            }
            for future in as_completed(futures):
                fid = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    log.warning("extraction failed for %s: %s", fid, exc)
                    on_result(fid, None, f"{type(exc).__name__}: {exc}")
                else:
                    on_result(fid, result, None)
                progress.advance(bar)

    def first_done(fid: str, doc: DocumentResult | None, error: str | None) -> None:
        if doc is None:
            failed[fid] = error or "unknown error"
        else:
            final[fid] = doc
        persist(fid, doc, error)

    run_pass(dict.fromkeys(contexts), f"Extracting {len(contexts)} documents", first_done)
    first_pass = dict(final)

    escalated = 0
    chosen: dict[str, dict[str, str]] = {}
    if escalate and first_pass:
        candidates = {fid: flags_for(doc) for fid, doc in first_pass.items()}
        candidates = {fid: f for fid, f in candidates.items() if f}
        cap = math.ceil(settings.max_escalation_share * len(first_pass))
        chosen = dict(sorted(candidates.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:cap])

    def second_done(fid: str, doc: DocumentResult | None, error: str | None) -> None:
        nonlocal escalated
        if doc is None:
            log.warning("escalation failed for %s (%s); keeping first-pass result", fid, error)
            return
        final[fid] = merge_escalation(first_pass[fid], doc, chosen[fid])
        escalated += 1
        persist(fid, final[fid])

    if chosen:
        run_pass(chosen, f"Escalating {len(chosen)} documents", second_done)

    with session_scope() as s:
        skipped = _mark_skipped(s)
        s.flush()
        duplicates = _copy_duplicates(s)

    found = [f for d in final.values() for f in d.fields.values() if f.status == "found"]
    return {
        "documents": len(final),
        "escalated": escalated,
        "failed": len(failed),
        "skipped": skipped,
        "duplicates": duplicates,
        "fields_found": len(found),
        "evidence_verified_share": round(sum(bool(f.verified) for f in found) / max(1, len(found)), 3),
    }
