"""Evaluation: field extraction vs hand labels, late interaction vs BM25 retrieval, and end-to-end chat answers."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from berkshire_contracts.config import get_settings
from berkshire_contracts.db import Contract, FieldEvidence, session_scope
from berkshire_contracts.evaluation.compare import (
    JUDGED_TEXT_FIELDS,
    deterministic_match,
    fuzzy_text_match,
    to_comparable,
)
from berkshire_contracts.extraction.fields import FIELD_BY_NAME
from berkshire_contracts.extraction.llm import LlmClient
from berkshire_contracts.schemas import FieldMatchBatch, JudgeVerdict

log = logging.getLogger(__name__)

JUDGE_PROMPT_VERSION = "judge-v2"
FIELD_JUDGE_INSTRUCTIONS = """You grade contract-extraction outputs against a reference answer written by a
reviewer from the document. For each item decide whether the prediction is equivalent to the reference: it must
state the same key facts (dollar amounts, limits, durations, counts, parties, scope). Different wording,
abbreviations or extra correct detail are fine. A missing or different key amount, limit, duration or subject is
not equivalent.
Field guidance:
- contract_description is a one-sentence summary of the goods or services. It is equivalent when it identifies
  the same goods or services, even if it omits qualifiers (site, program, purchasing vehicle) or describes the
  underlying services rather than what an amendment changes.
- insurance_requirements is equivalent only when the same coverage lines and limits appear; a missing coverage
  line or a different limit is not equivalent.
Return one verdict per item using the item's field name."""

CHAT_JUDGE_INSTRUCTIONS = """You grade answers from a contract Q&A assistant. You get the question, a reference
answer written by a reviewer from the documents, the assistant's answer, any result table, and the source
passages the assistant cited. correctness: correct = matches the reference on the key facts; partially_correct =
some key facts right, some missing or wrong; incorrect = wrong or unrelated. If the reference says the assistant
should decline or report missing information, an answer that clearly declines or says the information is not in
the documents is correct. grounded = every factual claim in the answer is supported by the cited passages or by
the evidence quotes shown in the answer or its result table (true for a refusal, including one that only names
related documents). abstained = the answer declines or says the documents don't contain the answer."""

MODES = ("bm25", "plaid", "exact", "plaid_rerank")


def _gt(name: str) -> dict:
    return json.loads((get_settings().ground_truth_dir / name).read_text())


def _accepts(spec: dict) -> list[Any]:
    return list(spec.get("accept", []))


# ---------------------------------------------------------------- extraction


def _predictions(
    file_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], FieldEvidence]]:
    with session_scope() as s:
        contracts = {c.file_id: c for c in s.query(Contract).filter(Contract.file_id.in_(file_ids))}
        preds = {
            fid: {name: to_comparable(name, getattr(c, name)) for name in FIELD_BY_NAME}
            for fid, c in contracts.items()
        }
        evidence = {
            (e.file_id, e.field_name): e
            for e in s.query(FieldEvidence).filter(FieldEvidence.file_id.in_(file_ids))
        }
        for e in evidence.values():
            s.expunge(e)
    return preds, evidence


def _judge_text_fields(items: list[dict], client: LlmClient | None) -> dict[str, bool]:
    if not items:
        return {}
    if client is not None:
        try:
            settings = get_settings()
            payload = json.dumps(items, indent=1)
            parsed, _ = client.parse(
                purpose="judge_fields",
                model=settings.judge_model,
                instructions=FIELD_JUDGE_INSTRUCTIONS,
                user_input=payload,
                schema=FieldMatchBatch,
                prompt_version=JUDGE_PROMPT_VERSION,
                reasoning_effort=settings.judge_reasoning_effort,
                max_output_tokens=6000,
            )
            verdicts = {v.field: v.equivalent for v in parsed.verdicts}
            if all(i["field"] in verdicts for i in items):
                return verdicts
        except Exception as exc:
            log.warning("field judge unavailable (%s); using fuzzy matching", exc)
    return {i["field"]: fuzzy_text_match(i["prediction"], i["reference"]) for i in items}


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


def evaluate_extraction(client: LlmClient | None) -> tuple[pd.DataFrame, dict[str, Any]]:
    gt = _gt("extraction_ground_truth.json")
    docs = gt["documents"]
    preds, evidence = _predictions([d["file_id"] for d in docs])
    rows: list[dict[str, Any]] = []
    for doc in docs:
        fid = doc["file_id"]
        pred = preds.get(fid, {})
        to_judge: list[dict] = []
        pending: list[dict] = []
        for field, spec in doc["fields"].items():
            expected = spec.get("value")
            expected = to_comparable(field, _coerce(field, expected))
            accepts = [to_comparable(field, _coerce(field, a)) for a in _accepts(spec)]
            predicted = pred.get(field)
            row = {
                "file_id": fid,
                "filename": doc["filename"],
                "categories": ",".join(doc.get("categories", [])),
                "field": field,
                "expected": expected,
                "accept": accepts,
                "predicted": predicted,
                "gt_page": spec.get("page"),
            }
            ev = evidence.get((fid, field))
            row["pred_page"] = ev.page if ev and ev.status == "found" else None
            row["verified"] = bool(ev.verified) if ev and ev.status == "found" else None
            row["confidence"] = ev.confidence if ev and ev.status == "found" else None
            candidates = [("value", expected)] + [
                (f"alt{i}", a) for i, a in enumerate(accepts) if a is not None
            ]
            decided: dict[str, bool] = {}
            for key, cand in candidates:
                if predicted is None or cand is None:
                    continue
                result = deterministic_match(field, predicted, cand)
                if result is None:
                    to_judge.append(
                        {"field": f"{field}::{key}", "reference": str(cand), "prediction": str(predicted)}
                    )
                else:
                    decided[key] = result
            row["_decided"] = decided
            pending.append(row)
        judged = _judge_text_fields(to_judge, client)
        for row in pending:
            decided = row.pop("_decided")
            for key, ok in judged.items():
                name, _, cand_key = key.partition("::")
                if name == row["field"]:
                    decided[cand_key] = ok
            match_value = decided.get("value", False)
            match_alt = any(v for k, v in decided.items() if k != "value")
            row["outcome"] = _outcome(
                match_value, match_alt, row["expected"], row["predicted"], row["accept"]
            )
            row["judged"] = row["field"] in JUDGED_TEXT_FIELDS and row["predicted"] is not None
            rows.append(row)
    frame = pd.DataFrame(rows)
    return frame, summarize_extraction(frame)


def _coerce(field: str, value: Any) -> Any:
    if value is None:
        return None
    kind = FIELD_BY_NAME[field].kind
    if kind == "date":
        return datetime.fromisoformat(str(value)).date()
    return value


CORRECT = {"correct", "correct_alt", "correct_null", "acceptable_null"}


def _rates(frame: pd.DataFrame) -> dict[str, float | int]:
    n = len(frame)
    if n == 0:
        return {"labels": 0}
    gt_present = frame["expected"].notna()
    pred_present = frame["predicted"].notna()
    good_value = frame["outcome"].isin({"correct", "correct_alt"}) & pred_present
    tp = int(good_value.sum())
    precision = tp / max(1, int(pred_present.sum()))
    recall = int((good_value & gt_present).sum()) / max(1, int(gt_present.sum()))
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    nulls = int((~gt_present).sum())
    return {
        "labels": n,
        "accuracy": round(frame["outcome"].isin(CORRECT).mean(), 3),
        "strict_accuracy": round(frame["outcome"].isin({"correct", "correct_null"}).mean(), 3),
        "value_precision": round(precision, 3),
        "value_recall": round(recall, 3),
        "value_f1": round(f1, 3),
        "hallucination_rate": round(int((frame["outcome"] == "false_positive").sum()) / max(1, nulls), 3),
        "misses": int((frame["outcome"] == "miss").sum()),
        "wrong": int((frame["outcome"] == "wrong").sum()),
        "false_positives": int((frame["outcome"] == "false_positive").sum()),
    }


def summarize_extraction(frame: pd.DataFrame) -> dict[str, Any]:
    overall = _rates(frame)
    found = frame[frame["predicted"].notna()]
    overall["evidence_verified"] = (
        round(found["verified"].fillna(False).astype(bool).mean(), 3) if len(found) else None
    )
    with_page = frame[
        frame["outcome"].isin({"correct", "correct_alt"})
        & frame["gt_page"].notna()
        & frame["pred_page"].notna()
    ]
    if len(with_page):
        diff = (with_page["pred_page"].astype(int) - with_page["gt_page"].astype(int)).abs()
        overall["citation_page_exact"] = round((diff == 0).mean(), 3)
        overall["citation_page_within_1"] = round((diff <= 1).mean(), 3)
    ok = found["outcome"].isin({"correct", "correct_alt"})
    levels = found["confidence"].fillna("unknown")
    by_confidence = [
        {
            "confidence": level,
            "values": int((levels == level).sum()),
            "accuracy": round(ok[levels == level].mean(), 3),
        }
        for level in ("high", "medium", "low", "unknown")
        if (levels == level).any()
    ]
    if len(found):
        overall["review_load_below_high"] = round((levels != "high").mean(), 3)
        overall["errors_caught_below_high"] = (
            round((levels[~ok] != "high").mean(), 3) if (~ok).any() else None
        )
    by_field = []
    for field, group in frame.groupby("field", sort=False):
        r = _rates(group)
        by_field.append(
            {
                "field": FIELD_BY_NAME[field].label,
                **{
                    k: r[k]
                    for k in ("labels", "accuracy", "value_precision", "value_recall", "hallucination_rate")
                },
            }
        )
    by_category = []
    categories = sorted({c for cats in frame["categories"] for c in cats.split(",") if c})
    for cat in categories:
        group = frame[frame["categories"].str.split(",").map(lambda cs, c=cat: c in cs)]
        r = _rates(group)
        by_category.append(
            {
                "category": cat,
                "documents": group["file_id"].nunique(),
                "labels": r["labels"],
                "accuracy": r["accuracy"],
                "value_f1": r["value_f1"],
            }
        )
    return {
        "overall": overall,
        "by_field": by_field,
        "by_category": by_category,
        "by_confidence": by_confidence,
    }


# ---------------------------------------------------------------- retrieval


def evaluate_retrieval(k: int = 10) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    from berkshire_contracts.retrieval.index import load_index

    index = load_index()
    queries = _gt("retrieval_queries.json")["queries"]
    rows = []
    index.encoder.encode_query("warm up")
    for mode in MODES:
        for q in queries:
            started = time.perf_counter()
            hits = index.search(q["query"], k=k, mode=mode, max_per_file=None, max_per_family=None)
            latency = (time.perf_counter() - started) * 1000
            relevant = {(r["file_id"], p) for r in q["relevant"] for p in r["pages"]}
            relevant_files = {r["file_id"] for r in q["relevant"]}
            page_ranks = [h.rank for h in hits if (h.chunk.file_id, h.chunk.page_no) in relevant]
            file_ranks = [h.rank for h in hits if h.chunk.file_id in relevant_files]
            first = min(page_ranks) if page_ranks else None
            rows.append(
                {
                    "mode": mode,
                    "query_id": q["id"],
                    "query": q["query"],
                    "first_relevant_rank": first,
                    "hit@1": bool(first and first <= 1),
                    "hit@5": bool(first and first <= 5),
                    "hit@10": bool(first and first <= 10),
                    "file_hit@5": bool(file_ranks and min(file_ranks) <= 5),
                    "rr": 1.0 / first if first else 0.0,
                    "latency_ms": round(latency, 1),
                    "top_result": f"{hits[0].chunk.filename} p.{hits[0].chunk.page_no}" if hits else "",
                }
            )
    frame = pd.DataFrame(rows)
    labels = {
        "bm25": "BM25 (lexical baseline)",
        "plaid": "PLAID only (compressed late interaction)",
        "exact": "Exact MaxSim (brute-force late interaction)",
        "plaid_rerank": "PLAID + exact MaxSim rerank (deployed)",
    }
    summary = []
    for mode in MODES:
        g = frame[frame["mode"] == mode]
        summary.append(
            {
                "retriever": labels[mode],
                "page_recall@1": round(g["hit@1"].mean(), 3),
                "page_recall@5": round(g["hit@5"].mean(), 3),
                "page_recall@10": round(g["hit@10"].mean(), 3),
                "file_recall@5": round(g["file_hit@5"].mean(), 3),
                "MRR@10": round(g["rr"].mean(), 3),
                "median_latency_ms": round(float(g["latency_ms"].median()), 1),
            }
        )
    return frame, summary


# ---------------------------------------------------------------- chat


def _judge_chat(case: dict, response, client: LlmClient | None) -> JudgeVerdict | None:
    if client is None:
        return None
    settings = get_settings()
    cited = [s for s in response.sources if s.sid in set(response.cited)] or response.sources[:3]
    passages = "\n\n".join(f"[{s.sid}] {s.filename} p.{s.page}\n{s.text}" for s in cited)
    table = ""
    if response.table is not None and len(response.table):
        table = "\nResult table (first 25 rows):\n" + response.table.head(25).to_csv(index=False)
    user_input = (
        f"Question: {case['question']}\n\nReference answer: {case['reference']}\n\n"
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
        log.warning("chat judge unavailable: %s", exc)
        return None


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


def evaluate_chat(client: LlmClient | None) -> tuple[pd.DataFrame, dict[str, Any]]:
    from berkshire_contracts.chat.service import ContractAssistant

    assistant = ContractAssistant()
    assistant.rate_limiter.per_minute = 10_000
    cases = _gt("chat_cases.json")["cases"]
    rows = []
    for case in cases:
        response = assistant.ask(case["question"], session="evaluation")
        cited_files = {s.file_id for s in response.sources if s.sid in set(response.cited)}
        if response.route == "structured":
            cited_files |= set(response.matched_file_ids)
        retrieved_files = {s.file_id for s in response.sources}
        expected_files = set(case.get("expected_file_ids", []))
        row: dict[str, Any] = {
            "id": case["id"],
            "type": case["type"],
            "question": case["question"],
            "route": response.route,
            "route_ok": response.route in case["expected_routes"],
            "latency_s": round(response.latency_ms / 1000, 2),
            "cost_usd": round(response.cost_usd, 5),
            "insufficient_evidence": response.insufficient_evidence,
            "error": response.error,
            "answer": response.answer[:1500],
        }
        if case["type"] == "structured" and expected_files:
            got = set(response.matched_file_ids)
            row["set_precision"] = round(len(got & expected_files) / len(got), 3) if got else 0.0
            row["set_recall"] = round(len(got & expected_files) / len(expected_files), 3)
        if "expected_plan" in case:
            row["plan_ok"] = _plan_matches(case["expected_plan"], response.plan)
        if expected_files and case["type"] != "structured":
            row["source_retrieved"] = bool(expected_files & retrieved_files)
            row["source_cited"] = bool(expected_files & cited_files)
        verdict = (
            None
            if case["type"] == "structured" and "expected_plan" in case
            else _judge_chat(case, response, client)
        )
        if verdict:
            row.update(
                {
                    "judge_correctness": verdict.correctness,
                    "judge_grounded": verdict.grounded,
                    "judge_abstained": verdict.abstained,
                    "judge_rationale": verdict.rationale,
                }
            )
        if case.get("expect_abstain"):
            row["abstained_ok"] = bool(
                response.route == "out_of_scope"
                or response.insufficient_evidence
                or (verdict and verdict.abstained)
            )
        rows.append(row)
    frame = pd.DataFrame(rows)
    judged = (
        frame[frame.get("judge_correctness", pd.Series(dtype=object)).notna()]
        if "judge_correctness" in frame
        else frame.iloc[0:0]
    )
    summary = {
        "cases": len(frame),
        "route_accuracy": round(frame["route_ok"].mean(), 3),
        "judge_correct": round((judged["judge_correctness"] == "correct").mean(), 3) if len(judged) else None,
        "judge_correct_or_partial": round(
            judged["judge_correctness"].isin(["correct", "partially_correct"]).mean(), 3
        )
        if len(judged)
        else None,
        "judge_grounded": round(judged["judge_grounded"].astype(bool).mean(), 3) if len(judged) else None,
        "abstention_correct": round(frame["abstained_ok"].dropna().astype(bool).mean(), 3)
        if "abstained_ok" in frame
        else None,
        "source_cited": round(frame["source_cited"].dropna().astype(bool).mean(), 3)
        if "source_cited" in frame
        else None,
        "plan_correct": round(frame["plan_ok"].dropna().astype(bool).mean(), 3)
        if "plan_ok" in frame
        else None,
        "median_latency_s": round(float(frame["latency_s"].median()), 2),
        "p90_latency_s": round(float(frame["latency_s"].quantile(0.9)), 2),
        "mean_cost_usd": round(float(frame["cost_usd"].mean()), 5),
        "errors": int(frame["error"].notna().sum()),
    }
    if "set_recall" in frame:
        s = frame.dropna(subset=["set_recall"])
        summary["structured_set_precision"] = round(float(s["set_precision"].mean()), 3)
        summary["structured_set_recall"] = round(float(s["set_recall"].mean()), 3)
    return frame, summary


# ---------------------------------------------------------------- reporting


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def write_report(
    extraction: dict, retrieval: list[dict], chat: dict, chat_frame: pd.DataFrame, meta: dict
) -> str:
    o = extraction["overall"]
    best = max(retrieval, key=lambda r: r["page_recall@5"]) if retrieval else None
    bm25 = next((r for r in retrieval if r["retriever"].startswith("BM25")), None)
    deployed = next((r for r in retrieval if "deployed" in r["retriever"]), None)
    lines = [
        "# Evaluation report",
        "",
        f"Generated {meta['generated_at']} · extraction model `{meta['extraction_model']}` · judge `{meta['judge_model']}` "
        f"(rubric {meta['judge_prompt_version']}) · retriever `{meta['colbert_model']}`.",
        "",
        "Ground truth was labeled from the source text by the AI assistant that built the pipeline, before extraction "
        "results were viewed; it has not yet been independently audited. Labels were not edited after results were "
        "seen; rubric judge-v2 added field guidance for descriptions after reviewing judge-v1 disagreements.",
        "",
        "## Field extraction",
        "",
        f"- {o['labels']} labeled fields across {meta['gt_documents']} documents (native, OCR, long-form, amendments, "
        "renewals, encrypted, upload-wrapper and null-heavy forms).",
        f"- Accuracy (value or accepted alternative, including correct blanks): **{_pct(o['accuracy'])}**; strict: {_pct(o['strict_accuracy'])}.",
        f"- Value precision {_pct(o['value_precision'])}, recall {_pct(o['value_recall'])}, F1 {_pct(o['value_f1'])}.",
        f"- Hallucination rate on fields the document does not state: **{_pct(o['hallucination_rate'])}**.",
        f"- Evidence quotes verified verbatim in the source: {_pct(o.get('evidence_verified'))}; cited page exact "
        f"{_pct(o.get('citation_page_exact'))}, within one page {_pct(o.get('citation_page_within_1'))}.",
        f"- Confidence as a review trigger: sending every value below high confidence to review "
        f"({_pct(o.get('review_load_below_high'))} of values) catches {_pct(o.get('errors_caught_below_high'))} of "
        "wrong values, so confidence alone is not a sufficient review gate (see the table below).",
        "",
        pd.DataFrame(extraction["by_field"]).to_markdown(index=False),
        "",
        pd.DataFrame(extraction["by_confidence"]).to_markdown(index=False),
        "",
        pd.DataFrame(extraction["by_category"]).to_markdown(index=False),
        "",
        "## Retrieval",
        "",
        f"{meta['retrieval_queries']} paraphrased questions with known answer pages; a hit is a chunk from the right page.",
        "",
        pd.DataFrame(retrieval).to_markdown(index=False),
        "",
    ]
    if bm25 and deployed:
        lines.append(
            f"Late interaction (deployed) finds the answer page in the top 5 for {_pct(deployed['page_recall@5'])} of "
            f"questions versus {_pct(bm25['page_recall@5'])} for BM25 (MRR {deployed['MRR@10']:.2f} vs {bm25['MRR@10']:.2f})."
        )
        lines.append("")
    if best:
        lines.append(f"Best retriever on page recall@5: {best['retriever']}.")
        lines.append("")
    lines += [
        "## End-to-end chat",
        "",
        f"- {chat['cases']} cases (structured, clause lookup, hybrid, negative and out-of-scope).",
        f"- Routing accuracy {_pct(chat['route_accuracy'])}; judge-rated correct {_pct(chat['judge_correct'])} "
        f"(correct or partially correct {_pct(chat['judge_correct_or_partial'])}); grounded {_pct(chat['judge_grounded'])}.",
        f"- Correct abstention on unanswerable questions: {_pct(chat['abstention_correct'])}; expected source cited: "
        f"{_pct(chat['source_cited'])}.",
        f"- Median latency {chat['median_latency_s']}s (p90 {chat['p90_latency_s']}s); mean cost ${chat['mean_cost_usd']:.4f} per question.",
        "",
        chat_frame[
            [
                c
                for c in (
                    "id",
                    "type",
                    "route",
                    "route_ok",
                    "judge_correctness",
                    "judge_grounded",
                    "abstained_ok",
                    "source_cited",
                    "set_recall",
                    "plan_ok",
                    "latency_s",
                )
                if c in chat_frame
            ]
        ].to_markdown(index=False),
        "",
    ]
    return "\n".join(lines)


def run_evaluation(use_judge: bool = True) -> dict[str, Any]:
    settings = get_settings()
    out = settings.eval_results_dir
    out.mkdir(parents=True, exist_ok=True)
    client = None
    if use_judge:
        try:
            client = LlmClient(settings)
        except RuntimeError as exc:
            log.warning("no OpenAI key; judge disabled (%s)", exc)

    ext_frame, ext_summary = evaluate_extraction(client)
    ext_frame.drop(columns=["accept"]).to_csv(out / "extraction_field_results.csv", index=False)
    ret_frame, ret_summary = evaluate_retrieval()
    ret_frame.to_csv(out / "retrieval_results.csv", index=False)
    chat_frame, chat_summary = evaluate_chat(client)
    chat_frame.to_csv(out / "chat_results.csv", index=False)

    gt = _gt("extraction_ground_truth.json")
    meta = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "extraction_model": settings.extraction_model,
        "judge_model": settings.judge_model if client else "none (fuzzy matching)",
        "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "colbert_model": settings.colbert_model,
        "gt_documents": len(gt["documents"]),
        "retrieval_queries": len(_gt("retrieval_queries.json")["queries"]),
    }
    report = write_report(ext_summary, ret_summary, chat_summary, chat_frame, meta)
    (out / "report.md").write_text(report)
    o = ext_summary["overall"]
    deployed = next(r for r in ret_summary if "deployed" in r["retriever"])
    bm25 = next(r for r in ret_summary if r["retriever"].startswith("BM25"))
    headline = (
        f"**Extraction:** {_pct(o['accuracy'])} field accuracy on {o['labels']} labeled fields "
        f"({meta['gt_documents']} documents), {_pct(o['hallucination_rate'])} hallucination rate on absent fields, "
        f"{_pct(o.get('evidence_verified'))} of quotes verified in the source.  \n"
        f"**Retrieval:** answer page in top 5 for {_pct(deployed['page_recall@5'])} of questions with late interaction "
        f"vs {_pct(bm25['page_recall@5'])} with BM25.  \n"
        f"**Chat:** {_pct(chat_summary['judge_correct_or_partial'])} judged correct or partially correct, "
        f"{_pct(chat_summary['abstention_correct'])} correct refusals, median {chat_summary['median_latency_s']}s per answer.  \n"
        f"<sub>Judge: {meta['judge_model']}. Ground truth labeled by the build agent; see eval/results/report.md.</sub>"
    )
    chat_cols = [
        c
        for c in (
            "id",
            "type",
            "question",
            "route",
            "judge_correctness",
            "judge_grounded",
            "abstained_ok",
            "source_cited",
            "latency_s",
        )
        if c in chat_frame
    ]
    summary = {
        "meta": meta,
        "extraction": ext_summary,
        "retrieval": ret_summary,
        "chat": chat_summary,
        "headline_markdown": headline,
        "extraction_by_field": ext_summary["by_field"],
        "chat_cases": json.loads(chat_frame[chat_cols].to_json(orient="records")),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return {
        "extraction_accuracy": o["accuracy"],
        "retrieval_recall@5": deployed["page_recall@5"],
        "bm25_recall@5": bm25["page_recall@5"],
        "chat_correct": chat_summary["judge_correct"],
    }
