"""Question answering: guardrails, routing, structured execution, late-interaction retrieval, grounded answers."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
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


class ContractAssistant:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.rate_limiter = SessionRateLimiter(self.settings.max_questions_per_minute)
        self._client: LlmClient | None = None

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
                response.notes.extend(structured.notes)
                response.matched_file_ids = structured.file_ids
                if structured.matched == 0 and names_contracts(plan):
                    response.notes.append(
                        "no extracted record matched, so the contract text was searched instead"
                    )
                    response.route = "clause_search"
                    hits = self._search(plan.search_query or question, None)
                    self._answer(question, hits, response, None)
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
                scope = set(scoped["file_id"]) if named and not scoped.empty else None
                if named and scoped.empty:
                    response.notes.append(
                        "couldn't match the named contract or vendor; searched all documents"
                    )
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
