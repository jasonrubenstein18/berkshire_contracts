import csv
import json
import threading
import time
from datetime import date

import numpy as np
import pandas as pd
import pytest

from berkshire_contracts.evaluation.compare import deterministic_match, to_comparable
from berkshire_contracts.evaluation.ground_truth import (
    ChatCase,
    UnverifiedLabels,
    load_chat_cases,
    load_extraction_labels,
)
from berkshire_contracts.evaluation.runner import (
    _outcome,
    _plan_matches,
    _rates,
    _table_evidence,
    score_chat,
    summarize_chat,
    write_grading_sheet,
)
from berkshire_contracts.retrieval import colbert
from berkshire_contracts.retrieval.colbert import maxsim, maxsim_many, maxsim_segments


def unit_rows(rng, n, dim=8):
    x = rng.normal(size=(n, dim)).astype(np.float32)
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def test_maxsim_matches_brute_force():
    rng = np.random.default_rng(0)
    query, doc = unit_rows(rng, 4), unit_rows(rng, 11)
    expected = sum(max(float(q @ d) for d in doc) for q in query)
    assert np.isclose(maxsim(query, doc), expected, atol=1e-5)


def test_segment_maxsim_equals_per_document_maxsim():
    rng = np.random.default_rng(1)
    query = unit_rows(rng, 5)
    docs = [unit_rows(rng, n) for n in (3, 9, 1, 6)]
    stacked = np.concatenate(docs)
    offsets = np.cumsum([0, *[len(d) for d in docs]])
    assert np.allclose(maxsim_segments(query, stacked, offsets), maxsim_many(query, docs), atol=1e-5)
    assert np.allclose(maxsim_many(query, docs), [maxsim(query, d) for d in docs], atol=1e-5)


def test_concurrent_first_calls_load_one_encoder(monkeypatch):
    built = []

    class SlowEncoder:
        def __init__(self):
            time.sleep(0.2)
            built.append(self)

    monkeypatch.setattr(colbert, "LateInteractionEncoder", SlowEncoder)
    colbert._cached_encoder.cache_clear()
    results = []
    threads = [threading.Thread(target=lambda: results.append(colbert.get_encoder())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    colbert._cached_encoder.cache_clear()
    assert len(built) == 1
    assert all(r is built[0] for r in results)


def test_field_comparators():
    assert deterministic_match("award_amount_usd", 1_000_400.0, 1_000_000.0)
    assert not deterministic_match("award_amount_usd", 1_100_000.0, 1_000_000.0)
    assert deterministic_match("vendor_name", "DLR Group Inc.", "DLR Group")
    assert deterministic_match("vendor_name", "Tyler Technologies, Inc.", "Tyler Technologies")
    assert not deterministic_match("vendor_name", "Motorola Solutions", "Tyler Technologies")
    assert deterministic_match("contract_number", "SOW 141301", "141301")
    assert deterministic_match("governing_law", "State of Illinois", "Illinois")
    assert deterministic_match("effective_date", "2025-04-04", "2025-04-04")
    assert not deterministic_match("document_type", "amendment_modification", "agreement")
    assert to_comparable("effective_date", date(2025, 4, 4)) == "2025-04-04"


def test_outcomes():
    assert _outcome(False, False, None, None, []) == "correct_null"
    assert _outcome(False, False, None, "made up", []) == "false_positive"
    assert _outcome(False, False, "x", None, [None]) == "acceptable_null"
    assert _outcome(False, False, "x", None, []) == "miss"
    assert _outcome(True, False, "x", "x", []) == "correct"
    assert _outcome(False, True, "x", "y", ["y"]) == "correct_alt"
    assert _outcome(False, False, "x", "z", []) == "wrong"


def test_plan_check():
    plan = {
        "metric": "list",
        "filters": [
            {
                "field": "expiration_date",
                "op": "between",
                "value": "2026-01-01",
                "value_to": "2026-12-31",
                "values": [],
            }
        ],
    }
    expected = {
        "filter_field": "expiration_date",
        "filter_value": "2026-01-01",
        "filter_value_to": "2026-12-31",
    }
    assert _plan_matches(expected, plan)
    assert not _plan_matches(expected | {"metric": "count"}, plan)
    assert not _plan_matches(expected, None)


LABEL_COLUMNS = [
    "file_id",
    "filename",
    "field",
    "value",
    "accept",
    "page",
    "quote",
    "notes",
    "reviewer_verdict",
    "reviewer_value",
    "changed_after_results",
]


def write_csv(path, columns, rows):
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


def test_label_loader_refuses_unverified_rows(tmp_path):
    rows = [
        {"file_id": "a", "field": "award_amount_usd", "value": "100", "reviewer_verdict": "agree"},
        {"file_id": "a", "field": "governing_law", "value": "Illinois"},
    ]
    write_csv(tmp_path / "extraction_labels.csv", LABEL_COLUMNS, rows)
    with pytest.raises(UnverifiedLabels, match="lines 3"):
        load_extraction_labels(tmp_path)


def test_label_loader_applies_corrections_and_alternatives(tmp_path):
    rows = [
        {
            "file_id": "a",
            "field": "effective_date",
            "value": "not stated",
            "accept": "2021-10-20",
            "reviewer_verdict": "agree",
        },
        {
            "file_id": "a",
            "field": "award_amount_usd",
            "value": "152550",
            "accept": "not stated; 132,630",
            "page": "2",
            "reviewer_verdict": "corrected",
            "reviewer_value": "$140,000",
        },
        {"file_id": "a", "field": "auto_renewal", "value": "false", "reviewer_verdict": "Agree"},
    ]
    write_csv(tmp_path / "extraction_labels.csv", LABEL_COLUMNS, rows)
    effective, award, auto = load_extraction_labels(tmp_path)
    assert effective.value is None and effective.accept == ("2021-10-20",)
    assert award.value == 140_000.0 and award.accept == (None, 132_630.0) and award.page == 2
    assert auto.value is False


def test_label_loader_reads_spreadsheet_formatting(tmp_path):
    rows = [
        {"file_id": "a", "field": "effective_date", "value": "1/1/25", "reviewer_verdict": "agree"},
        {"file_id": "a", "field": "expiration_date", "value": "12/31/2025", "reviewer_verdict": "agree"},
        {"file_id": "a", "field": "auto_renewal", "value": "FALSE", "reviewer_verdict": "agree"},
    ]
    write_csv(tmp_path / "extraction_labels.csv", LABEL_COLUMNS, rows)
    text = (tmp_path / "extraction_labels.csv").read_text()
    (tmp_path / "extraction_labels.csv").write_text("\ufeff" + text, encoding="utf-8")
    effective, expiration, auto = load_extraction_labels(tmp_path)
    assert (effective.value, expiration.value, auto.value) == ("2025-01-01", "2025-12-31", False)


def test_corrected_label_needs_a_value(tmp_path):
    rows = [{"file_id": "a", "field": "governing_law", "value": "Illinois", "reviewer_verdict": "corrected"}]
    write_csv(tmp_path / "extraction_labels.csv", LABEL_COLUMNS, rows)
    with pytest.raises(ValueError, match="reviewer_value"):
        load_extraction_labels(tmp_path)


def test_chat_cases_expand_a_file_set(tmp_path):
    columns = [
        "id",
        "type",
        "question",
        "reference_answer",
        "expected_file_ids",
        "optional_file_ids",
        "expected_pages",
        "should_refuse",
        "expected_plan",
        "notes",
        "reviewer_verdict",
    ]
    write_csv(
        tmp_path / "chat_cases.csv",
        columns,
        [
            {"id": "q1", "type": "structured", "expected_file_ids": "@set.csv", "reviewer_verdict": "agree"},
            {
                "id": "q2",
                "type": "clause",
                "expected_file_ids": "b",
                "expected_pages": "b:5; c:7",
                "should_refuse": "false",
                "reviewer_verdict": "agree",
            },
        ],
    )
    write_csv(
        tmp_path / "set.csv",
        ["file_id", "in_set", "reviewer_verdict"],
        [
            {"file_id": "x", "in_set": "yes", "reviewer_verdict": "agree"},
            {"file_id": "y", "in_set": "optional", "reviewer_verdict": "agree"},
            {"file_id": "z", "in_set": "no", "reviewer_verdict": "corrected"},
        ],
    )
    q1, q2 = load_chat_cases(tmp_path)
    assert q1.expected_files == ("x",) and q1.optional_files == ("y",)
    assert q2.expected_pages == (("b", 5), ("c", 7)) and not q2.should_refuse


def test_extraction_rates_separate_invented_values_from_blanks():
    frame = pd.DataFrame(
        [
            {"outcome": "correct", "predicted": "1", "stated": True, "blank_ok": False},
            {"outcome": "miss", "predicted": "not stated", "stated": True, "blank_ok": False},
            {"outcome": "acceptable_null", "predicted": "not stated", "stated": True, "blank_ok": True},
            {"outcome": "correct_null", "predicted": "not stated", "stated": False, "blank_ok": False},
            {"outcome": "false_positive", "predicted": "2", "stated": False, "blank_ok": False},
        ]
    )
    r = _rates(frame)
    assert r["accuracy"] == 0.6
    assert r["precision"] == 0.5 and r["recall"] == 0.5
    assert r["invented"] == 1 and r["invented_rate"] == 0.5


def _case(id, should_refuse=False, pages=(), plan=None, type="clause"):
    return ChatCase(id, type, f"question {id}", "ref", (), (), tuple(pages), should_refuse, plan)


def _run_row(id, route="clause_search", insufficient=False, sources=(), cited=(), judge=("", "", "")):
    return {
        "id": id,
        "question": f"question {id}",
        "run_at": "now",
        "route": route,
        "plan": "",
        "answer": "answer",
        "insufficient_evidence": str(insufficient),
        "sources": json.dumps(
            [{"sid": f"S{i + 1}", "file_id": f, "filename": f, "page": p} for i, (f, p) in enumerate(sources)]
        ),
        "cited": json.dumps(list(cited)),
        "matched_file_ids": "[]",
        "matched_files": "",
        "latency_s": "2.0",
        "cost_usd": "0.001",
        "error": "",
        "judge_correct": judge[0],
        "judge_supported": judge[1],
        "judge_declined": judge[2],
    }


def test_chat_scoring_counts_refusals_both_ways_and_skips_support_for_refusals():
    cases = [
        _case("answered", pages=[("a", 3)]),
        _case("wrongly_declined", pages=[("b", 1)]),
        _case("declined_ok", should_refuse=True),
        _case("off_topic", should_refuse=True),
    ]
    run = pd.DataFrame(
        [
            _run_row("answered", sources=[("x", 1), ("a", 3)], cited=["S2"], judge=("incorrect", "no", "no")),
            _run_row(
                "wrongly_declined", insufficient=True, sources=[("z", 9)], judge=("incorrect", "yes", "yes")
            ),
            _run_row("declined_ok", insufficient=True, judge=("correct", "yes", "yes")),
            _run_row("off_topic", route="out_of_scope"),
        ]
    )
    grades = {"answered": {"human_correct": "correct", "human_supported": "yes", "human_declined": "no"}}
    frame = score_chat(run, cases, grades)
    rows = frame.set_index("id")

    assert rows.loc["answered", "correct"] == "correct" and rows.loc["answered", "graded_by"] == "human"
    assert rows.loc["answered", "supported"] == "yes"
    assert rows.loc["answered", "pages_found"] == 1 and rows.loc["answered", "best_rank"] == 2
    assert rows.loc["answered", "files_cited"] == 1
    assert not rows.loc["wrongly_declined", "refusal_ok"]
    assert rows.loc["wrongly_declined", "supported"] == "n/a"
    assert rows.loc["wrongly_declined", "pages_found"] == 0
    assert rows.loc["declined_ok", "refusal_ok"] and rows.loc["declined_ok", "supported"] == "n/a"
    assert rows.loc["off_topic", "declined"]

    s = summarize_chat(frame)
    assert (s["correct_declines"], s["should_decline"]) == (2, 2)
    assert (s["wrong_declines"], s["answerable"]) == (1, 2)
    assert (s["supported"], s["supported_of"]) == (1, 1)
    assert s["graded_by"] == "mixed"
    assert s["judge_vs_human"]["correct"] == {"compared": 1, "agree": 0}


def test_structured_answers_are_credited_for_the_evidence_they_show():
    table = pd.DataFrame(
        {
            "File": ["mod2.pdf", "base.pdf"],
            "Award amount (USD) evidence": ["p.1: “total not to exceed $140,524”", ""],
            "Contract # evidence": ["p.1: “21144”", "p.3: “#21144”"],
        }
    )
    pages = _table_evidence(table, {"m": "mod2.pdf", "b": "base.pdf"})
    assert pages == [("m", 1), ("b", 3)]
    row = _run_row("q5", route="structured") | {"table_evidence": json.dumps(pages)}
    frame = score_chat(pd.DataFrame([row]), [_case("q5", pages=[("m", 1)], type="clause")], {})
    assert frame.loc[0, "pages_found"] == 1 and pd.isna(frame.loc[0, "best_rank"])


def test_plan_only_case_is_not_graded_for_correctness():
    plan = {"filter_field": "expiration_date", "filter_value": "2026-01-01", "filter_value_to": "2026-12-31"}
    row = _run_row("q2", route="structured") | {
        "plan": json.dumps(
            {"filters": [{"field": "expiration_date", "value": "2026-01-01", "value_to": "2026-12-31"}]}
        )
    }
    frame = score_chat(pd.DataFrame([row]), [_case("q2", plan=plan, type="structured")], {})
    assert frame.loc[0, "filter_ok"] and frame.loc[0, "correct"] == "n/a"
    assert summarize_chat(frame)["answer_cases"] == 0


def test_grading_sheet_is_never_overwritten(tmp_path):
    cases = [_case("q1")]
    run = pd.DataFrame([_run_row("q1", sources=[("a", 1)], cited=["S1"])])
    path = tmp_path / "chat_grading.csv"
    write_grading_sheet(run, cases, path)
    sheet = pd.read_csv(path, dtype=str, keep_default_na=False)
    assert sheet.loc[0, "cited_sources"] == "[S1] a p.1" and sheet.loc[0, "human_correct"] == ""
    sheet.loc[0, "human_correct"] = "correct"
    sheet.to_csv(path, index=False)
    with pytest.raises(RuntimeError, match="already has grades"):
        write_grading_sheet(run, cases, path)
