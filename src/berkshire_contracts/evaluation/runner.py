"""Evaluation against the human-verified test sets in eval/ground_truth.

Extraction is scored deterministically against the field labels. Chat questions are run fresh (cached model output
is never reused), checked automatically for sources, refusals and filters, and graded by a person in
chat_grading.csv. An AI grader gives a second opinion so its agreement with the person can be measured.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from berkshire_contracts.chat.filters import FIELD_LABELS
from berkshire_contracts.config import get_settings
from berkshire_contracts.db import Contract, CorpusFile, FieldEvidence, session_scope
from berkshire_contracts.evaluation.compare import deterministic_match, to_comparable
from berkshire_contracts.evaluation.ground_truth import (
    ChatCase,
    FieldLabel,
    labels_changed_after_results,
    load_chat_cases,
    load_extraction_labels,
)
from berkshire_contracts.extraction.fields import FIELD_BY_NAME
from berkshire_contracts.extraction.llm import LlmClient
from berkshire_contracts.schemas import JudgeVerdict

log = logging.getLogger(__name__)

JUDGE_PROMPT_VERSION = "judge-v3"
CHAT_JUDGE_INSTRUCTIONS = """You grade answers from a contract Q&A assistant against a reference answer that a
reviewer wrote from the documents. You get the question, the reference, the assistant's answer, and the source
passages it cited.
correctness: correct = states the reference's key facts (amounts, dates, durations, notice periods, parties)
without contradicting it; extra correct detail is fine. partially_correct = some key facts right, others missing
or wrong. incorrect = wrong, unrelated, or declines when the reference gives an answer. If the reference says the
assistant should decline, an answer that clearly declines or says the documents don't contain it is correct.
grounded = every factual claim in the answer is supported by the cited passages or by evidence quotes shown in
the answer; set true when the answer only declines.
abstained = the answer declines or says the documents don't contain the answer, even if it names related
documents."""

EXTRACTION_RESULTS = "extraction_results.csv"
CHAT_RESULTS = "chat_results.csv"
CHAT_GRADING = "chat_grading.csv"
GRADING_VIEW = "chat_grading.md"
FAILURE_NOTES = "failure_notes.csv"
REVIEW_NOTES = "review_notes.md"
GRADE_VALUES = {
    "human_correct": {"correct", "partially_correct", "incorrect", "n/a"},
    "human_supported": {"yes", "no", "n/a"},
    "human_declined": {"yes", "no"},
}
CORRECT = {"correct", "correct_alt", "correct_null", "acceptable_null"}
RIGHT_VALUE = {"correct", "correct_alt"}


# ---------------------------------------------------------------- extraction


def _outcome(match_value: bool, match_alt: bool, expected: Any, predicted: Any, accepts: list[Any]) -> str:
    if expected is None:
        if predicted is None:
            return "correct_null"
        return "correct_alt" if match_alt else "false_positive"
    if predicted is None:
        return "acceptable_null" if None in accepts else "miss"
    if match_value:
        return "correct"
    return "correct_alt" if match_alt else "wrong"


def _show(value: Any) -> str:
    if value is None:
        return "not stated"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def score_extraction(labels: list[FieldLabel]) -> pd.DataFrame:
    file_ids = sorted({lb.file_id for lb in labels})
    with session_scope() as s:
        predictions = {
            c.file_id: {f: to_comparable(f, getattr(c, f)) for f in FIELD_BY_NAME}
            for c in s.query(Contract).filter(Contract.file_id.in_(file_ids))
        }
        evidence = {
            (e.file_id, e.field_name): (e.page, e.verified, e.confidence)
            for e in s.query(FieldEvidence).filter(
                FieldEvidence.file_id.in_(file_ids), FieldEvidence.status == "found"
            )
        }
        profiles = dict(
            s.query(CorpusFile.file_id, CorpusFile.text_profile)
            .filter(CorpusFile.file_id.in_(file_ids))
            .all()
        )
    missing = [f for f in file_ids if f not in predictions]
    if missing:
        log.warning("no extracted row for %s; every field counts as blank", ", ".join(missing))
    rows = []
    for lb in labels:
        predicted = predictions.get(lb.file_id, {}).get(lb.field)
        page, verified, confidence = (
            evidence.get((lb.file_id, lb.field), (None, None, None)) if predicted is not None else (None,) * 3
        )
        match_value = (
            lb.value is not None
            and predicted is not None
            and deterministic_match(lb.field, predicted, lb.value)
        )
        match_alt = predicted is not None and any(
            a is not None and deterministic_match(lb.field, predicted, a) for a in lb.accept
        )
        rows.append(
            {
                "file_id": lb.file_id,
                "filename": lb.filename,
                "text": profiles.get(lb.file_id, "unknown"),
                "field": lb.field,
                "expected": _show(lb.value),
                "accept": "; ".join(_show(a) for a in lb.accept),
                "predicted": _show(predicted),
                "outcome": _outcome(match_value, match_alt, lb.value, predicted, list(lb.accept)),
                "stated": lb.value is not None,
                "blank_ok": None in lb.accept,
                "label_page": lb.page,
                "predicted_page": page,
                "quote_verified": verified,
                "confidence": confidence,
                "changed_after_results": lb.changed_after_results,
            }
        )
    return pd.DataFrame(rows)


def _rates(frame: pd.DataFrame) -> dict[str, Any]:
    outcome = frame["outcome"]
    filled = frame["predicted"] != "not stated"
    right = outcome.isin(RIGHT_VALUE)
    required = frame["stated"] & ~frame["blank_ok"]
    not_stated = ~frame["stated"]
    return {
        "labels": len(frame),
        "correct": int(outcome.isin(CORRECT).sum()),
        "accuracy": round(float(outcome.isin(CORRECT).mean()), 3) if len(frame) else None,
        "filled": int(filled.sum()),
        "precision": round(float((right & filled).sum() / filled.sum()), 3) if filled.any() else None,
        "required": int(required.sum()),
        "recall": round(float((right & required).sum() / required.sum()), 3) if required.any() else None,
        "not_stated": int(not_stated.sum()),
        "invented": int((outcome == "false_positive").sum()),
        "invented_rate": round(float((outcome == "false_positive").sum() / not_stated.sum()), 3)
        if not_stated.any()
        else None,
        "missed": int((outcome == "miss").sum()),
        "wrong": int((outcome == "wrong").sum()),
    }


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / (1 + z * z / n)
    return (round(centre - half, 3), round(centre + half, 3))


def summarize_extraction(frame: pd.DataFrame) -> dict[str, Any]:
    overall = _rates(frame)
    overall["documents"] = int(frame["file_id"].nunique())
    overall["accuracy_95ci"] = _wilson(overall["correct"], overall["labels"])
    filled = frame[frame["predicted"] != "not stated"]
    overall["quote_verified"] = (
        round(float(filled["quote_verified"].fillna(False).astype(bool).mean()), 3) if len(filled) else None
    )
    paged = frame[
        frame["outcome"].isin(RIGHT_VALUE) & frame["label_page"].notna() & frame["predicted_page"].notna()
    ]
    overall["page_checked"] = len(paged)
    overall["page_exact"] = (
        round(float((paged["label_page"].astype(int) == paged["predicted_page"].astype(int)).mean()), 3)
        if len(paged)
        else None
    )
    overall["changed_after_results"] = int((frame["changed_after_results"].fillna("") != "").sum())
    by_field = [
        {"field": FIELD_BY_NAME[name].label, **_rates(group)}
        for name, group in frame.groupby("field", sort=False)
    ]
    by_text = [
        {"text": profile, "documents": int(group["file_id"].nunique()), **_rates(group)}
        for profile, group in frame.groupby("text")
    ]
    levels = filled["confidence"].fillna("unknown")
    by_confidence = [
        {
            "confidence": level,
            "values": int((levels == level).sum()),
            "right": round(float(filled["outcome"][levels == level].isin(RIGHT_VALUE).mean()), 3),
        }
        for level in ("high", "medium", "low", "unknown")
        if (levels == level).any()
    ]
    return {"overall": overall, "by_field": by_field, "by_text": by_text, "by_confidence": by_confidence}


# ---------------------------------------------------------------- chat run


def _judge(case: ChatCase, response, client: LlmClient) -> JudgeVerdict | None:
    settings = get_settings()
    cited = [s for s in response.sources if s.sid in set(response.cited)] or response.sources[:3]
    passages = "\n\n".join(f"[{s.sid}] {s.filename} p.{s.page}\n{s.text}" for s in cited)
    table = ""
    if response.table is not None and len(response.table):
        table = "\nResult table (first 25 rows):\n" + response.table.head(25).to_csv(index=False)
    if not passages and table:
        passages = "(none: this answer comes from the extracted table; its evidence quotes are shown inline)"
    user_input = (
        f"Question: {case.question}\n\nReference answer: {case.reference}\n\n"
        f"Assistant answer:\n{response.answer}\n{table}\nCited passages:\n{passages or '(none)'}"
    )
    try:
        verdict, _ = client.parse(
            purpose="judge_chat",
            model=settings.judge_model,
            instructions=CHAT_JUDGE_INSTRUCTIONS,
            user_input=user_input,
            schema=JudgeVerdict,
            prompt_version=JUDGE_PROMPT_VERSION,
            reasoning_effort=settings.judge_reasoning_effort,
            max_output_tokens=4000,
        )
        return verdict
    except Exception as exc:
        log.warning("AI grader unavailable for %s: %s", case.id, exc)
        return None


def _filenames(file_ids: list[str]) -> dict[str, str]:
    with session_scope() as s:
        return dict(s.query(Contract.file_id, Contract.filename).filter(Contract.file_id.in_(file_ids)).all())


def _table_evidence(table: pd.DataFrame | None, names: dict[str, str]) -> list[tuple[str, int]]:
    """(file_id, page) for every evidence quote shown in a structured result table."""
    if table is None or FIELD_LABELS["filename"] not in table:
        return []
    ids = {name: fid for fid, name in names.items()}
    columns = [c for c in table.columns if str(c).endswith(" evidence")]
    pages = []
    for _, row in table.iterrows():
        fid = ids.get(row[FIELD_LABELS["filename"]])
        for col in columns:
            pages += [(fid, int(p)) for p in re.findall(r"p\.(\d+):", str(row[col] or "")) if fid]
    return list(dict.fromkeys(pages))


def run_chat(cases: list[ChatCase], client: LlmClient, use_judge: bool) -> pd.DataFrame:
    from berkshire_contracts.chat.service import ContractAssistant

    assistant = ContractAssistant(client=client)
    assistant.rate_limiter.per_minute = 10_000
    run_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    for case in cases:
        log.info("chat case %s: %s", case.id, case.question)
        response = assistant.ask(case.question, session="evaluation")
        verdict = _judge(case, response, client) if use_judge and case.expected_plan is None else None
        names = _filenames(response.matched_file_ids)
        rows.append(
            {
                "id": case.id,
                "question": case.question,
                "run_at": run_at,
                "route": response.route,
                "plan": json.dumps(response.plan, default=str) if response.plan else "",
                "answer": response.answer,
                "insufficient_evidence": response.insufficient_evidence,
                "sources": json.dumps(
                    [
                        {
                            "sid": s.sid,
                            "file_id": s.file_id,
                            "filename": s.filename,
                            "page": s.page,
                            "score": round(s.score, 4),
                        }
                        for s in response.sources
                    ]
                ),
                "cited": json.dumps(response.cited),
                "matched_file_ids": json.dumps(response.matched_file_ids),
                "matched_files": "; ".join(names.get(f, f) for f in response.matched_file_ids),
                "table_evidence": json.dumps(_table_evidence(response.table, names)),
                "latency_s": round(response.latency_ms / 1000, 2),
                "cost_usd": round(response.cost_usd, 5),
                "error": response.error or "",
                "judge_correct": verdict.correctness if verdict else "",
                "judge_supported": ("yes" if verdict.grounded else "no") if verdict else "",
                "judge_declined": ("yes" if verdict.abstained else "no") if verdict else "",
                "judge_rationale": verdict.rationale if verdict else "",
            }
        )
    return pd.DataFrame(rows)


def _grading_note(case: ChatCase) -> str:
    if case.expected_plan is not None:
        return "Filter check only (scored automatically). Leave human_correct n/a; fill human_declined."
    if case.should_refuse:
        return (
            "Should decline. human_declined = yes if it says the documents don't contain this (a list showing "
            "'not stated' for every document counts). human_correct = correct if it declined without presenting "
            "other terms as the answer. human_supported = n/a when it declines."
        )
    return (
        "Compare with the reference: correct, partially_correct or incorrect. human_supported = yes only if every "
        "claim is backed by a cited page or an evidence quote shown in the answer; n/a if it declined. "
        "human_declined = yes if it said the documents don't answer it."
    )


def read_grades(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    grades = {}
    for line, r in enumerate(rows, start=2):
        g = {k: (r.get(k) or "").strip().lower() for k in GRADE_VALUES}
        for key, value in g.items():
            if value and value not in GRADE_VALUES[key]:
                allowed = ", ".join(sorted(GRADE_VALUES[key]))
                raise ValueError(f"{path.name} line {line}: {key} is {value!r}; use one of {allowed}")
        if any(g.values()):
            grades[r["id"]] = g
    return grades


def write_grading_sheet(run: pd.DataFrame, cases: list[ChatCase], path: Path) -> None:
    if read_grades(path):
        raise RuntimeError(f"{path.name} already has grades; refusing to overwrite them")
    by_id = {c.id: c for c in cases}
    rows = []
    for r in run.to_dict("records"):
        case = by_id[r["id"]]
        sources = json.loads(r["sources"])
        cited = set(json.loads(r["cited"]))
        evidence = json.loads(r.get("table_evidence") or "[]")
        names = _filenames(sorted({f for f, _ in evidence})) if evidence else {}
        shown = [f"[{s['sid']}] {s['filename']} p.{s['page']}" for s in sources if s["sid"] in cited]
        shown += [f"{names.get(f, f)} p.{p} (table evidence)" for f, p in evidence]
        rows.append(
            {
                "id": r["id"],
                "question": r["question"],
                "reference_answer": case.reference,
                "answer": r["answer"],
                "cited_sources": "; ".join(shown),
                "matched_files": r["matched_files"],
                "how_to_grade": _grading_note(case),
                "human_correct": "",
                "human_supported": "",
                "human_declined": "",
                "notes": "",
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")
    _write_grading_view(rows, path.with_name(GRADING_VIEW))


def _write_grading_view(rows: list[dict[str, str]], path: Path) -> None:
    lines = [
        "# Chat answers to grade",
        "",
        f"Read each answer here, then enter grades in `{CHAT_GRADING}` (human_correct, human_supported, "
        "human_declined). The AI grader's verdicts are deliberately not shown.",
        "",
    ]
    for r in rows:
        answer = "\n".join(f"> {line}" for line in r["answer"].splitlines())
        lines += [
            "---",
            "",
            f"## {r['id']}: {r['question']}",
            "",
            f"**Reference answer:** {r['reference_answer']}",
            "",
            f"**How to grade:** {r['how_to_grade']}",
            "",
            "**Assistant's answer:**",
            "",
            answer,
            "",
            f"**Cited pages:** {r['cited_sources'] or 'none'}",
            "",
        ]
    path.write_text("\n".join(lines))


# ---------------------------------------------------------------- chat scoring


def _plan_matches(expected: dict, plan: dict | None) -> bool:
    if not plan:
        return False
    if "metric" in expected and plan.get("metric") != expected["metric"]:
        return False
    for cond in plan.get("filters", []):
        if cond["field"] != expected["filter_field"]:
            continue
        value_ok = cond.get("value") == expected.get("filter_value") or expected.get("filter_value") in (
            cond.get("values") or []
        )
        upper_ok = "filter_value_to" not in expected or cond.get("value_to") == expected["filter_value_to"]
        if value_ok and upper_ok:
            return True
    return False


def _yes(value: Any) -> bool:
    return str(value).strip().lower() in {"true", "yes", "1"}


def score_chat(run: pd.DataFrame, cases: list[ChatCase], grades: dict[str, dict[str, str]]) -> pd.DataFrame:
    by_id = {r["id"]: r for r in run.fillna("").to_dict("records")}
    rows = []
    for case in cases:
        r = by_id.get(case.id)
        if r is None:
            raise RuntimeError(f"{case.id} is not in {CHAT_RESULTS}; rerun `contracts evaluate`")
        sources = json.loads(r["sources"] or "[]")
        cited_sids = set(json.loads(r["cited"] or "[]"))
        matched = json.loads(r["matched_file_ids"] or "[]")
        retrieved = [(s["file_id"], int(s["page"])) for s in sources]
        shown = {(f, int(p)) for f, p in json.loads(r.get("table_evidence") or "[]")}
        cited_files = {s["file_id"] for s in sources if s["sid"] in cited_sids}
        if r["route"] == "structured":
            cited_files |= set(matched)
        row: dict[str, Any] = {
            "id": case.id,
            "type": case.type,
            "question": case.question,
            "should_decline": case.should_refuse,
            "route": r["route"],
            "latency_s": float(r["latency_s"] or 0),
            "cost_usd": float(r["cost_usd"] or 0),
            "error": r["error"],
        }
        if case.expected_pages:
            ranks = [retrieved.index(p) + 1 for p in case.expected_pages if p in retrieved]
            files = {f for f, _ in case.expected_pages}
            row |= {
                "pages_expected": len(case.expected_pages),
                "pages_found": sum(p in retrieved or p in shown for p in case.expected_pages),
                "best_rank": min(ranks) if ranks else None,
                "files_expected": len(files),
                "files_cited": len(files & cited_files),
            }
        if case.expected_plan is not None:
            row["filter_ok"] = _plan_matches(case.expected_plan, json.loads(r["plan"]) if r["plan"] else None)
        if case.type == "structured" and case.expected_files:
            expected = set(case.expected_files)
            got = set(matched) - set(case.optional_files)
            row |= {
                "set_returned": len(got),
                "set_expected": len(expected),
                "set_precision": round(len(got & expected) / len(got), 3) if got else 0.0,
                "set_recall": round(len(got & expected) / len(expected), 3),
                "set_extra": "; ".join(sorted(got - expected)),
                "set_missing": "; ".join(sorted(expected - got)),
            }

        human = grades.get(case.id, {})
        system_declined = r["route"] == "out_of_scope" or _yes(r["insufficient_evidence"])
        if human.get("human_declined"):
            declined = human["human_declined"] == "yes"
        else:
            declined = system_declined or r["judge_declined"] == "yes"
        if case.expected_plan is not None:
            correct, graded_by = "n/a", "automatic"
        elif human.get("human_correct"):
            correct, graded_by = human["human_correct"], "human"
        elif r["judge_correct"]:
            correct, graded_by = r["judge_correct"], "AI grader"
        else:
            correct, graded_by = "", ""
        if declined or case.expected_plan is not None:
            supported = "n/a"
        else:
            supported = human.get("human_supported") or r["judge_supported"]
        row |= {
            "declined": declined,
            "refusal_ok": declined == case.should_refuse,
            "correct": correct,
            "supported": supported,
            "graded_by": graded_by,
            "judge_correct": r["judge_correct"],
            "judge_supported": "n/a" if declined else r["judge_supported"],
            "judge_declined": r["judge_declined"],
            "human_correct": human.get("human_correct", ""),
            "human_supported": human.get("human_supported", ""),
            "human_declined": human.get("human_declined", ""),
        }
        rows.append(row)
    return pd.DataFrame(rows)


def _agreement(frame: pd.DataFrame, human: str, judge: str) -> dict[str, int]:
    both = frame[
        (frame[human] != "") & (frame[judge] != "") & (frame[human] != "n/a") & (frame[judge] != "n/a")
    ]
    return {"compared": len(both), "agree": int((both[human] == both[judge]).sum())}


def summarize_chat(frame: pd.DataFrame) -> dict[str, Any]:
    answerable = frame[~frame["should_decline"]]
    unanswerable = frame[frame["should_decline"]]
    graded = answerable[answerable["correct"].isin(["correct", "partially_correct", "incorrect"])]
    answer_cases = answerable[answerable["correct"] != "n/a"]
    supported = answerable[answerable["supported"].isin(["yes", "no"])]
    paged = frame.dropna(subset=["pages_expected"]) if "pages_expected" in frame else frame.iloc[0:0]
    graders = set(graded["graded_by"])
    summary: dict[str, Any] = {
        "cases": len(frame),
        "by_type": {str(k): int(v) for k, v in frame["type"].value_counts().items()},
        "answer_cases": len(answer_cases),
        "graded": len(graded),
        "graded_by": graders.pop() if len(graders) == 1 else ("mixed" if graders else "nobody"),
        "correct": int((graded["correct"] == "correct").sum()),
        "partially_correct": int((graded["correct"] == "partially_correct").sum()),
        "supported": int((supported["supported"] == "yes").sum()),
        "supported_of": len(supported),
        "pages_expected": int(paged["pages_expected"].sum()) if len(paged) else 0,
        "pages_found": int(paged["pages_found"].sum()) if len(paged) else 0,
        "files_expected": int(paged["files_expected"].sum()) if len(paged) else 0,
        "files_cited": int(paged["files_cited"].sum()) if len(paged) else 0,
        "should_decline": len(unanswerable),
        "correct_declines": int(unanswerable["declined"].sum()),
        "answerable": len(answerable),
        "wrong_declines": int(answerable["declined"].sum()),
        "median_latency_s": round(float(frame["latency_s"].median()), 2),
        "p90_latency_s": round(float(frame["latency_s"].quantile(0.9)), 2),
        "mean_cost_usd": round(float(frame["cost_usd"].mean()), 5),
        "total_cost_usd": round(float(frame["cost_usd"].sum()), 4),
        "errors": int((frame["error"] != "").sum()),
        "judge_vs_human": {
            "correct": _agreement(frame, "human_correct", "judge_correct"),
            "supported": _agreement(frame, "human_supported", "judge_supported"),
            "declined": _agreement(frame, "human_declined", "judge_declined"),
        },
    }
    if "filter_ok" in frame:
        checked = frame.dropna(subset=["filter_ok"])
        summary["filter_ok"] = int(checked["filter_ok"].astype(bool).sum())
        summary["filter_checked"] = len(checked)
    if "set_recall" in frame:
        sets = frame.dropna(subset=["set_recall"])
        summary["list_precision"] = round(float(sets["set_precision"].mean()), 3)
        summary["list_recall"] = round(float(sets["set_recall"].mean()), 3)
    return summary


# ---------------------------------------------------------------- orchestration


def read_failure_notes(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return {
            r["item"].strip(): {
                "cause": (r.get("cause") or "").strip(),
                "category": (r.get("category") or "").strip(),
            }
            for r in csv.DictReader(fh)
            if r.get("item")
        }


def run_evaluation(report_only: bool = False, use_judge: bool = True) -> dict[str, Any]:
    from berkshire_contracts.evaluation.report import build_summary, write_report

    settings = get_settings()
    out = settings.eval_results_dir
    out.mkdir(parents=True, exist_ok=True)
    labels = load_extraction_labels()
    cases = load_chat_cases()

    extraction = score_extraction(labels)
    extraction.to_csv(out / EXTRACTION_RESULTS, index=False, encoding="utf-8-sig")

    run_path, grading_path = out / CHAT_RESULTS, out / CHAT_GRADING
    if report_only:
        if not run_path.exists():
            raise RuntimeError(f"no {CHAT_RESULTS} yet; run `contracts evaluate` first")
    else:
        if read_grades(grading_path):
            raise RuntimeError(
                f"{CHAT_GRADING} already has grades for the last run. Use --report-only to rescore it, "
                "or move the file aside to run the chat questions again."
            )
        client = LlmClient(settings, read_cache=False)
        run = run_chat(cases, client, use_judge)
        run.to_csv(run_path, index=False, encoding="utf-8-sig")
        write_grading_sheet(run, cases, grading_path)
    run = pd.read_csv(run_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    chat = score_chat(run, cases, read_grades(grading_path))

    meta = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "chat_run_at": run["run_at"].iloc[0] if len(run) else "",
        "extraction_model": settings.extraction_model,
        "router_model": settings.router_model,
        "chat_model": settings.chat_model,
        "judge_model": settings.judge_model,
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "colbert_model": settings.colbert_model,
        "labels_changed_after_results": labels_changed_after_results(),
    }
    summary = build_summary(
        summarize_extraction(extraction),
        extraction,
        summarize_chat(chat),
        chat,
        read_failure_notes(out / FAILURE_NOTES),
        meta,
    )
    notes_path = out / REVIEW_NOTES
    summary["review_notes"] = notes_path.read_text().strip() if notes_path.exists() else ""
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out / "report.md").write_text(write_report(summary))
    o, c = summary["extraction"]["overall"], summary["chat"]
    return {
        "extraction_accuracy": o["accuracy"],
        "invented_rate": o["invented_rate"],
        "chat_correct": f"{c['correct']}/{c['answer_cases']} ({c['graded_by']})",
        "correct_declines": f"{c['correct_declines']}/{c['should_decline']}",
        "wrong_declines": f"{c['wrong_declines']}/{c['answerable']}",
        "ungraded": c["answer_cases"] - c["graded"],
    }
