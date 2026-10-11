import pandas as pd
import pytest

from berkshire_contracts.chat import service
from berkshire_contracts.chat.answer import MIN_RELEVANCE, AnswerResult, answer_question
from berkshire_contracts.chat.filters import (
    apply_filter,
    dedupe_awards,
    names_contracts,
    resolve_scope,
    run_structured,
    vendor_groups,
)
from berkshire_contracts.chat.router import fallback_plan
from berkshire_contracts.extraction.llm import CallStats, LlmError
from berkshire_contracts.retrieval.index import ChunkMeta, SearchHit
from berkshire_contracts.schemas import FilterCondition, GroundedAnswer, QueryPlan


def frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "file_id": ["a", "b", "c", "d"],
            "filename": [
                "14234_Tyler_Hosting.pdf",
                "24213_Sympro.pdf",
                "16249_Motorola_Lease.pdf",
                "VDS.pdf",
            ],
            "family_id": ["14234", "24213", "16249", None],
            "contract_number": ["14234", "24213", None, None],
            "document_type": ["agreement", "agreement", "lease", "vendor_disclosure"],
            "vendor_name": ["Tyler Technologies, Inc.", "Sympro, Inc.", "Motorola Solutions", None],
            "auto_renewal": [True, True, False, None],
            "termination_for_convenience": [True, None, False, None],
            "needs_review": [False, False, True, False],
            "renewal_notice_days": [90, 60, None, None],
            "award_amount_usd": [1_000_000.0, 50_000.0, 250_000.0, None],
            "effective_date": pd.to_datetime(["2015-01-01", "2024-07-01", "2016-05-01", None]),
            "expiration_date": pd.to_datetime(["2026-03-31", "2027-06-30", "2026-11-30", None]),
            "renewal_type": ["automatic", "automatic", "county_option", None],
        }
    )


def cond(field, op, value=None, value_to=None, values=()):
    return FilterCondition(field=field, op=op, value=value, value_to=value_to, values=list(values))


def plan(**kw) -> QueryPlan:
    base = {
        "intent": "structured",
        "filters": [],
        "answer_fields": [],
        "group_by": None,
        "metric": "list",
        "sort_by": None,
        "sort_desc": False,
        "limit": None,
        "chart": "none",
        "search_query": None,
        "scope_contract_numbers": [],
        "scope_vendor_names": [],
        "explanation": "test",
    }
    return QueryPlan(**(base | kw))


def test_boolean_date_numeric_and_text_filters():
    df = frame()
    assert list(apply_filter(df, cond("auto_renewal", "is_true"))[0].file_id) == ["a", "b"]
    assert list(apply_filter(df, cond("auto_renewal", "equals", "no"))[0].file_id) == ["c"]
    in_2026 = apply_filter(df, cond("expiration_date", "between", "2026-01-01", "2026-12-31"))[0]
    assert set(in_2026.file_id) == {"a", "c"}
    assert list(apply_filter(df, cond("award_amount_usd", "greater_than", "$100,000"))[0].file_id) == [
        "a",
        "c",
    ]
    assert list(apply_filter(df, cond("vendor_name", "equals", "tyler technologies"))[0].file_id) == ["a"]
    assert list(apply_filter(df, cond("document_type", "in_list", values=["lease"]))[0].file_id) == ["c"]


def test_unusable_filters_are_reported_not_silently_applied():
    df = frame()
    out, note = apply_filter(df, cond("expiration_date", "greater_than", "next year"))
    assert len(out) == len(df) and "needs a valid date" in note
    out, note = apply_filter(df, cond("auto_renewal", "contains", "maybe"))
    assert len(out) == len(df) and "not a yes/no" in note


def test_scope_resolution_by_number_and_vendor():
    df = frame()
    assert list(resolve_scope(df, ["16249"], [])["file_id"]) == ["c"]
    assert list(resolve_scope(df, [], ["Motorola"])["file_id"]) == ["c"]
    assert resolve_scope(df, ["99999"], []).empty


def test_product_names_match_file_names_and_short_names_match_whole_words():
    df = frame().assign(
        vendor_name=[
            "Tyler Technologies, Inc.",
            "Emphasys Computer Solutions",
            "Motorola Solutions",
            "CTI Group",
        ]
    )
    assert list(resolve_scope(df, [], ["Sympro"])["file_id"]) == ["b"]
    assert list(resolve_scope(df, [], ["CTI"])["file_id"]) == ["d"]
    assert resolve_scope(df.assign(vendor_name="Lake Construction Co."), [], ["CTI"]).empty


def test_answer_fields_show_values_and_unmatched_names_return_nothing():
    df = frame()
    shown = run_structured(
        plan(scope_vendor_names=["Motorola"], answer_fields=["renewal_type", "renewal_notice_days"]), df
    )
    assert "Renewal type: **County option**" in shown.summary
    assert "Renewal notice (days): not stated in this document" in shown.summary

    named = plan(scope_vendor_names=["Starbucks"], filters=[cond("vendor_name", "equals", "Starbucks")])
    missing = run_structured(named, df)
    assert missing.matched == 0 and any("no extracted record matches Starbucks" in n for n in missing.notes)
    assert names_contracts(named) and not names_contracts(plan(filters=[cond("auto_renewal", "is_true")]))

    many = pd.concat([df] * 3, ignore_index=True)
    many["file_id"] = [f"f{i}" for i in range(len(many))]
    spread = run_structured(plan(answer_fields=["renewal_notice_days"]), many).summary
    assert "90 days in 3" in spread and "60 days in 3" in spread and "6 don't say" in spread


def test_structured_list_count_and_grouping():
    df = frame()
    listed = run_structured(plan(filters=[cond("auto_renewal", "is_true")]), df)
    assert listed.matched == 2 and set(listed.file_ids) == {"a", "b"}
    assert "Auto-renewal" in listed.table.columns
    assert list(listed.table["Auto-renewal"]) == ["Yes", "Yes"]
    assert any("1 of 4 documents don't state auto-renewal" in n for n in listed.notes)
    assert "**Tyler Technologies, Inc.**" in listed.summary and "`24213_Sympro.pdf`" in listed.summary

    counted = run_structured(plan(metric="count", filters=[cond("document_type", "equals", "agreement")]), df)
    assert counted.summary.startswith("**2**")

    grouped = run_structured(plan(metric="count", group_by="expiration_year", chart="bar"), df)
    assert dict(zip(grouped.table.iloc[:, 0], grouped.table.iloc[:, 1], strict=True)) == {
        "2026": 2,
        "2027": 1,
        "Unknown": 1,
    }
    assert grouped.chart is not None


def test_vendor_variants_merge_and_restated_awards_count_once():
    names = pd.Series(
        [
            "Joseph J Henderson & Sons, Inc.",
            "Joseph J. Henderson & Son, Inc.",
            "William T. Connelly, Inc. dba Connelly Electric Co.",
            None,
            "Motorola Solutions",
        ]
    )
    groups = vendor_groups(names)
    assert groups[0] == groups[1]
    assert groups[2] == "William T. Connelly, Inc. dba Connelly Electric Co."
    assert pd.isna(groups[3]) and groups[4] == "Motorola Solutions"

    df = pd.DataFrame(
        {
            "file_id": ["award", "loa", "task", "other"],
            "family_id": ["24210", "24210", "24210", "23159"],
            "vendor_name": [
                "Joseph J Henderson & Sons, Inc.",
                "Joseph J. Henderson & Son, Inc.",
                "Joseph J. Henderson & Son, Inc.",
                "AGAE Contractors, Inc.",
            ],
            "award_amount_usd": [12_614_625.0, 12_614_625.0, 50_000.0, 2_000_000.0],
        }
    )
    df["vendor_group"] = vendor_groups(df["vendor_name"])
    df["award_group"] = df["family_id"] + "|" + df["vendor_group"]
    assert dedupe_awards(df)["award_amount_usd"].sum() == 14_614_625.0
    total = run_structured(
        plan(metric="sum_award"), df.assign(expiration_date=pd.NaT, filename="f", contract_number=None)
    )
    assert "$14,614,625" in total.summary and "2 distinct awards" in total.summary


def hit(relevance: float, text: str = "The agreement renews automatically each year.") -> SearchHit:
    chunk = ChunkMeta("a:1:0", "a", "14234_Tyler_Hosting.pdf", "14234", 1, None, text, text)
    return SearchHit(chunk, 30.0, 0.95, 1, relevance=relevance)


class FakeClient:
    def __init__(self, answer: GroundedAnswer | Exception):
        self.answer = answer
        self.calls = 0

    def parse(self, **kwargs):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer, CallStats(model="fake", cost_usd=0.001)


def test_answer_abstains_without_calling_model_when_evidence_is_weak():
    client = FakeClient(
        GroundedAnswer(answer="x", cited_sources=[], insufficient_evidence=False, follow_up_suggestions=[])
    )
    assert answer_question("q", [], client).insufficient_evidence
    weak = answer_question("q", [hit(MIN_RELEVANCE - 0.05)], client)
    assert weak.insufficient_evidence and client.calls == 0


def test_answer_strips_citations_to_sources_that_do_not_exist():
    client = FakeClient(
        GroundedAnswer(
            answer="It renews yearly [S1] and is capped [S7].",
            cited_sources=["S1", "S7"],
            insufficient_evidence=False,
            follow_up_suggestions=["a", "b", "c", "d"],
        )
    )
    result = answer_question("Does it renew?", [hit(0.93)], client)
    assert result.cited == ["S1"]
    assert "[S7]" not in result.answer and "[S1]" in result.answer
    assert len(result.follow_ups) == 3


def test_uncited_answers_carry_a_warning():
    client = FakeClient(
        GroundedAnswer(
            answer="It renews.", cited_sources=[], insufficient_evidence=False, follow_up_suggestions=[]
        )
    )
    assert "did not cite" in answer_question("q", [hit(0.93)], client).answer


@pytest.fixture()
def assistant(monkeypatch):
    bot = service.ContractAssistant()
    bot._client = object()
    monkeypatch.setattr(service, "spend_today", lambda purposes=None: 0.0)
    return bot


def test_guardrails_reject_empty_long_and_rapid_questions(assistant):
    assert assistant.ask("   ").route == "rejected"
    assert assistant.ask("x" * (assistant.settings.max_question_chars + 1)).route == "rejected"
    assistant.rate_limiter.per_minute = 1
    assert assistant.rate_limiter.allow("s")
    assert "faster than the demo allows" in assistant.ask("Again?", session="s").answer


def test_out_of_scope_and_planner_failure_paths(assistant, monkeypatch):
    def out_of_scope(question, client, budget_usd=None):
        p = fallback_plan(question, "x").model_copy(update={"intent": "out_of_scope"})
        return p, CallStats(model="fake", cost_usd=0.0)

    monkeypatch.setattr(service, "plan_question", out_of_scope)
    response = assistant.ask("What's the weather in Chicago?")
    assert response.route == "out_of_scope" and response.insufficient_evidence

    def broken_planner(question, client, budget_usd=None):
        raise LlmError("planner down")

    class FakeIndex:
        def search(self, query, k, file_ids=None, mode=None):
            return [hit(0.93)]

    monkeypatch.setattr(service, "plan_question", broken_planner)
    monkeypatch.setattr(service, "load_index", lambda: FakeIndex())
    monkeypatch.setattr(service, "load_contracts_frame", frame)

    def broken_answer(*args, **kwargs):
        raise LlmError("answer model down")

    monkeypatch.setattr(service, "answer_question", broken_answer)
    degraded = assistant.ask("Which contracts renew automatically?")
    assert degraded.route == "clause_search"
    assert "planner was unavailable" in " ".join(degraded.notes)
    assert "Showing the most relevant passages" in degraded.answer and degraded.sources


def test_unmatched_named_contract_falls_back_to_text_search(assistant, monkeypatch):
    named = plan(scope_vendor_names=["Starbucks"])
    monkeypatch.setattr(service, "plan_question", lambda q, c, budget_usd=None: (named, CallStats("m")))
    monkeypatch.setattr(service, "run_structured", lambda p: run_structured(p, frame()))

    class FakeIndex:
        def search(self, query, k, file_ids=None, mode=None):
            assert file_ids is None
            return [hit(0.93)]

    monkeypatch.setattr(service, "load_index", lambda: FakeIndex())
    monkeypatch.setattr(
        service,
        "answer_question",
        lambda *a, **k: AnswerResult("Not in these documents.", [], True, [], None),
    )
    response = assistant.ask("What liability cap applies to the Starbucks agreement?")
    assert response.route == "clause_search" and response.insufficient_evidence
    assert "no extracted record matched, so the contract text was searched instead" in response.notes


def test_missing_index_fails_gracefully(assistant, monkeypatch):
    monkeypatch.setattr(
        service, "plan_question", lambda q, c, budget_usd=None: (fallback_plan(q, "x"), CallStats("m"))
    )
    monkeypatch.setattr(service, "load_contracts_frame", frame)

    def missing():
        raise FileNotFoundError("no index")

    monkeypatch.setattr(service, "load_index", missing)
    response = assistant.ask("What is the liability cap?")
    assert "isn't available yet" in response.answer and response.error
