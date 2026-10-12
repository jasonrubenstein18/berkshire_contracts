"""Question answering: guardrails, routing, structured execution, late-interaction retrieval, grounded answers."""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import pandas as pd
import plotly.graph_objects as go

from berkshire_contracts.chat.answer import AnswerResult, answer_question
from berkshire_contracts.chat.filters import (
    StructuredResult,
    load_contracts_frame,
    names_contracts,
    resolve_scope,
    run_structured,
)
from berkshire_contracts.chat.router import fallback_plan, plan_question
from berkshire_contracts.config import get_settings
from berkshire_contracts.db import ChatLog, session_scope
from berkshire_contracts.extraction.llm import BudgetExceeded, LlmClient, LlmError, spend_today
from berkshire_contracts.retrieval.index import SearchHit, load_index
from berkshire_contracts.schemas import QueryPlan

log = logging.getLogger(__name__)

CHAT_PURPOSES = ("route", "answer")
OUT_OF_SCOPE_MESSAGE = (
    "I can only answer questions about these contracts: renewal terms, expirations, award "
    "amounts, termination and liability clauses, insurance, vendors, and so on. Try “Which contracts "
    "auto-renew?” or “What does the Motorola lease say about renewal notice?”"
)


@dataclass
class Source:
    sid: str
    file_id: str
    filename: str
    page: int
    section: str | None
    score: float
    text: str


@dataclass
class AssistantResponse:
    answer: str
    route: str
    plan: dict[str, Any] | None = None
    table: pd.DataFrame | None = None
    chart: go.Figure | None = None
    sources: list[Source] = field(default_factory=list)
    cited: list[str] = field(default_factory=list)
    matched_file_ids: list[str] = field(default_factory=list)
    insufficient_evidence: bool = False
    follow_ups: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    latency_ms: int = 0
    cost_usd: float = 0.0
    error: str | None = None


class SessionRateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, session: str) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events[session]
            while events and now - events[0] > 60:
                events.popleft()
            if len(events) >= self.per_minute:
                return False
            events.append(now)
            return True


def _sources(hits: list[SearchHit]) -> list[Source]:
    return [
        Source(
            f"S{i}",
            h.chunk.file_id,
            h.chunk.filename,
            h.chunk.page_no,
            h.chunk.section,
            h.relevance if h.relevance is not None else h.normalized,
            h.chunk.text,
        )
        for i, h in enumerate(hits, start=1)
    ]


def _passages_only(hits: list[SearchHit], reason: str) -> str:
    lines = [f"_{reason} Showing the most relevant passages instead._", ""]
    for i, h in enumerate(hits[:5], start=1):
        snippet = " ".join(h.chunk.text.split())[:320]
        lines.append(f"- **[S{i}] {h.chunk.filename}, p.{h.chunk.page_no}**: {snippet}…")
    return "\n".join(lines)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


@cache
def _file_words(index: Any) -> dict[str, frozenset[str]]:
    """Every word on each file's indexed pages and in its file name."""
    words: dict[str, set[str]] = defaultdict(set)
    for chunk in index.chunks:
        words[chunk.file_id].update(_words(chunk.text))
        words[chunk.file_id].update(_words(chunk.filename))
    return {file_id: frozenset(w) for file_id, w in words.items()}


def _unique(names: list[str]) -> list[str]:
    unique: dict[str, str] = {}
    for name in names:
        if name and name.strip():
            unique.setdefault(name.strip().lower(), name.strip())
    return list(unique.values())


def _named(plan: QueryPlan) -> tuple[list[str], list[str]]:
    """Contract numbers and vendor names the plan points at, from its scope and its filters."""

    def filtered(fields: set[str]) -> list[str]:
        return [
            value
            for c in plan.filters
            if c.field in fields and c.op in {"equals", "contains", "in_list"}
            for value in [c.value, *c.values]
            if value
        ]

    numbers = _unique([*plan.scope_contract_numbers, *filtered({"contract_number", "family_id"})])
    vendors = _unique([*plan.scope_vendor_names, *filtered({"vendor_name"})])
    return numbers, vendors


DESCRIPTIVE_FIELDS = {"document_type", "contract_description"}


def _without_descriptive_filters(plan: QueryPlan) -> tuple[QueryPlan, list[str]] | None:
    """The plan minus the planner's restatements of what the named contract is ("Modification 2", "agreement"),
    which often don't match how the document was recorded. None unless a contract number or vendor is named."""
    numbers, vendors = _named(plan)
    dropped = [c for c in plan.filters if c.field in DESCRIPTIVE_FIELDS]
    if not (numbers or vendors) or not dropped:
        return None
    kept = [c for c in plan.filters if c.field not in DESCRIPTIVE_FIELDS]
    labels = [
        f"{c.field.replace('_', ' ')} {c.op.replace('_', ' ')} {c.value or ', '.join(c.values)}"
        for c in dropped
    ]
    return plan.model_copy(update={"filters": kept}), labels


def _without_names(question: str, names: list[str]) -> str:
    """The question minus the vendor and contract names, which otherwise dominate the search for the clause."""
    text = question
    for name in sorted(names, key=len, reverse=True):
        stripped = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
        if stripped == text:
            for word in _words(name):
                stripped = re.sub(rf"\b{re.escape(word)}\b", " ", stripped, flags=re.IGNORECASE)
        text = stripped
    text = re.sub(r"(^|\s)['’]s\b", " ", text)
    text = re.sub(r"\s+([?.,!])", r"\1", re.sub(r"\s+", " ", text)).strip()
    return text if len(_words(text)) >= 3 else question


def _not_found(names: list[str]) -> str:
    quoted = " or ".join(f"“{n}”" for n in names)
    return (
        f"I couldn't find {quoted} in these documents (no vendor, file name or page mentions it), so I won't "
        "answer from other contracts. Browse vendors and contract numbers in the Contract explorer tab."
    )


class ContractAssistant:
    def __init__(self, client: LlmClient | None = None) -> None:
        self.settings = get_settings()
        self.rate_limiter = SessionRateLimiter(self.settings.max_questions_per_minute)
        self._client = client

    @property
    def client(self) -> LlmClient:
        if self._client is None:
            self._client = LlmClient(self.settings)
        return self._client

    def _route(self, question: str, response: AssistantResponse) -> QueryPlan:
        try:
            plan, stats = plan_question(question, self.client, budget_usd=self.settings.daily_llm_budget_usd)
            response.cost_usd += stats.cost_usd or 0.0
            return plan
        except BudgetExceeded:
            response.notes.append("daily AI budget reached; answering with search only")
            return fallback_plan(question, "AI budget reached")
        except (LlmError, RuntimeError) as exc:
            log.warning("routing failed: %s", exc)
            response.notes.append("the question planner was unavailable; searched contract text directly")
            return fallback_plan(question, "planner unavailable")

    def _search(self, query: str, file_ids: set[str] | None) -> list[SearchHit]:
        index = load_index()
        return index.search(query, k=8, file_ids=file_ids, mode=self.settings.retrieval_mode)

    def _search_unmatched(self, question: str, plan: QueryPlan, response: AssistantResponse) -> None:
        """No record matched the plan. Search only the named contracts' text: searching everything hands the answer
        model other contracts' clauses, which it may attribute to the named one."""
        numbers, vendors = _named(plan)
        names = [*vendors, *numbers]
        query = plan.search_query or _without_names(question, names)
        if not names:
            response.notes.append("no extracted record matched, so the contract text was searched instead")
            self._answer(question, self._search(query, None), response, None)
            return
        label = ", ".join(names)
        named = resolve_scope(load_contracts_frame(), numbers, vendors)
        if not named.empty:
            response.notes.append(
                f"none of the documents for {label} matched every condition, so their text was searched instead"
            )
            context = f"The sources are limited to the documents for {label}; answer only about them."
            self._answer(question, self._search(query, set(named["file_id"])), response, context)
            return

        files = _file_words(load_index())
        corpus = frozenset().union(*files.values())
        unknown = [name for name in names if any(w not in corpus for w in _words(name))]
        mentions = {
            file_id
            for file_id, words in files.items()
            if any(all(w in words for w in _words(name)) for name in names)
        }
        if unknown or not mentions:
            response.answer = _not_found(unknown or names)
            response.insufficient_evidence = True
            response.notes.append("no document mentions the named vendor or contract")
            return
        response.notes.append(
            f"couldn't match {label} to a contract record; searched the documents that mention it"
        )
        context = (
            f"No document record matched {label}; only use a source if it is actually from that contract."
        )
        self._answer(question, self._search(query, mentions), response, context)

    def _answer(
        self, question: str, hits: list[SearchHit], response: AssistantResponse, note: str | None
    ) -> None:
        response.sources = _sources(hits)
        try:
            result: AnswerResult = answer_question(
                question, hits, self.client, context_note=note, budget_usd=self.settings.daily_llm_budget_usd
            )
        except BudgetExceeded:
            response.answer = _passages_only(hits, "The daily AI budget has been reached.")
            return
        except (LlmError, RuntimeError) as exc:
            log.warning("answer synthesis failed: %s", exc)
            response.answer = _passages_only(hits, "The AI answer service is unavailable right now.")
            response.error = "answer synthesis unavailable"
            return
        response.answer = result.answer
        response.cited = result.cited
        response.insufficient_evidence = result.insufficient_evidence
        response.follow_ups = result.follow_ups
        if result.stats:
            response.cost_usd += result.stats.cost_usd or 0.0
        if result.insufficient_evidence and result.stats is None:
            response.sources = response.sources[:3]

    def ask(self, question: str, session: str = "default") -> AssistantResponse:
        started = time.perf_counter()
        question = (question or "").strip()
        response = AssistantResponse(answer="", route="rejected")
        try:
            if not question:
                response.answer = "Please type a question about the contracts."
                return response
            if len(question) > self.settings.max_question_chars:
                response.answer = (
                    f"Please keep questions under {self.settings.max_question_chars} characters."
                )
                return response
            if not self.rate_limiter.allow(session):
                response.answer = "You're asking questions faster than the demo allows. Please wait a minute."
                return response
            if spend_today(CHAT_PURPOSES) >= self.settings.daily_llm_budget_usd:
                response.notes.append("daily AI budget reached")

            plan = self._route(question, response)
            response.route = plan.intent
            response.plan = plan.model_dump()

            if plan.intent == "out_of_scope":
                response.answer = OUT_OF_SCOPE_MESSAGE
                response.insufficient_evidence = True
                return response

            structured: StructuredResult | None = None
            if plan.intent in {"structured", "hybrid"}:
                structured = run_structured(plan)
                relaxed = _without_descriptive_filters(plan) if structured.matched == 0 else None
                if relaxed is not None:
                    retry = run_structured(relaxed[0])
                    if retry.matched:
                        structured = retry
                        numbers, vendors = _named(plan)
                        response.notes.append(
                            f"no document for {', '.join([*vendors, *numbers])} matched {'; '.join(relaxed[1])}, "
                            "so all of its documents are shown"
                        )
                response.notes.extend(structured.notes)
                response.matched_file_ids = structured.file_ids
                if structured.matched == 0 and names_contracts(plan):
                    response.route = "clause_search"
                    self._search_unmatched(question, plan, response)
                    return response
                if plan.intent == "structured":
                    response.answer = structured.summary
                    response.table, response.chart = structured.table, structured.chart
                    if structured.matched == 0:
                        response.insufficient_evidence = True
                    return response

            if plan.intent == "hybrid":
                assert structured is not None
                if structured.matched == 0:
                    response.answer = (
                        f"None of these contracts match ({plan.explanation.rstrip('.')}), so there is "
                        "no clause to quote. Browse vendors and contract numbers in the Contract explorer tab."
                    )
                    response.insufficient_evidence = True
                    return response
                response.table = structured.table
                scope = set(structured.file_ids)
                note = f"The documents were pre-filtered: {plan.explanation}"
            else:
                df = load_contracts_frame()
                scoped = resolve_scope(df, plan.scope_contract_numbers, plan.scope_vendor_names)
                named = plan.scope_contract_numbers or plan.scope_vendor_names
                if named and scoped.empty:
                    self._search_unmatched(question, plan, response)
                    return response
                scope = set(scoped["file_id"]) if named else None
                note = None

            hits = self._search(plan.search_query or question, scope)
            self._answer(question, hits, response, note)
            return response
        except FileNotFoundError as exc:
            response.answer = "The contract search index isn't available yet. Please try again shortly."
            response.error = str(exc)
            return response
        except Exception as exc:
            log.exception("question failed")
            response.answer = "Something went wrong answering that question. Please rephrase or try again."
            response.error = f"{type(exc).__name__}: {exc}"
            return response
        finally:
            response.latency_ms = int((time.perf_counter() - started) * 1000)
            self._log(session, question, response)

    def _log(self, session: str, question: str, response: AssistantResponse) -> None:
        try:
            with session_scope() as s:
                s.add(
                    ChatLog(
                        session=session[:64],
                        question=question[:2000],
                        route=response.route,
                        plan=response.plan,
                        retrieved=[
                            {
                                "sid": src.sid,
                                "file_id": src.file_id,
                                "page": src.page,
                                "score": round(src.score, 4),
                            }
                            for src in response.sources
                        ],
                        answer=response.answer[:8000],
                        insufficient_evidence=response.insufficient_evidence,
                        top_score=response.sources[0].score if response.sources else None,
                        latency_ms=response.latency_ms,
                        cost_usd=round(response.cost_usd, 6),
                        error=response.error,
                    )
                )
        except Exception:
            log.exception("could not write chat log")
