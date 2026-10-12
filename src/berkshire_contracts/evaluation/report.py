"""summary.json (read by the app and the deck) and the one-page report.md."""

from __future__ import annotations

from typing import Any

import pandas as pd

from berkshire_contracts.extraction.fields import FIELD_BY_NAME

OUTCOME_WORDS = {"miss": "left blank", "wrong": "wrong value", "false_positive": "invented a value"}
GRADED_BY = {
    "human": "graded by a person",
    "AI grader": "graded by the AI grader (human grades pending)",
    "mixed": "graded partly by a person and partly by the AI grader",
    "nobody": "not yet graded",
}


def _pct(v: float | None) -> str:
    return "n/a" if v is None or pd.isna(v) else f"{v:.0%}"


def _short(filename: str, n: int = 44) -> str:
    return filename if len(filename) <= n else filename[: n - 1] + "…"


def _note(notes: dict[str, dict[str, str]], item: str) -> dict[str, str]:
    note = notes.get(item, {})
    return {"cause": note.get("cause", ""), "category": note.get("category", "")}


def _extraction_failures(frame: pd.DataFrame, notes: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    failed = frame[frame["outcome"].isin(OUTCOME_WORDS)]
    return [
        {
            "item": f"{r.file_id} {r.field}",
            "document": r.filename,
            "field": FIELD_BY_NAME[r.field].label,
            "problem": f"{OUTCOME_WORDS[r.outcome]}: expected {r.expected}, got {r.predicted}",
            **_note(notes, f"{r.file_id} {r.field}"),
        }
        for r in failed.itertuples()
    ]


def _chat_problems(r: dict[str, Any]) -> list[str]:
    problems = []
    if r["error"]:
        problems.append(f"error: {r['error']}")
    if not r["refusal_ok"]:
        problems.append(
            "declined an answerable question" if r["declined"] else "answered instead of declining"
        )
    if r["correct"] in {"partially_correct", "incorrect"}:
        problems.append(r["correct"].replace("_", " "))
    if r["supported"] == "no":
        problems.append("claims not backed by the cited pages")
    if pd.notna(r.get("pages_expected")) and r["pages_found"] < r["pages_expected"]:
        problems.append(f"expected page retrieved {int(r['pages_found'])}/{int(r['pages_expected'])}")
    if pd.notna(r.get("files_expected")) and r["files_cited"] < r["files_expected"]:
        problems.append(f"expected document cited {int(r['files_cited'])}/{int(r['files_expected'])}")
    if pd.notna(r.get("filter_ok")) and not r["filter_ok"]:
        problems.append("wrong filter")
    if isinstance(r.get("set_missing"), str) and r["set_missing"]:
        problems.append(f"missed {len(r['set_missing'].split('; '))} of the expected contracts")
    if isinstance(r.get("set_extra"), str) and r["set_extra"]:
        problems.append(f"returned {len(r['set_extra'].split('; '))} contracts outside the expected set")
    return problems


def _cell(r: dict[str, Any]) -> dict[str, str]:
    declined = "yes" if r["declined"] else "no"
    if r["should_decline"]:
        declined += " (should decline)"
    pages = ""
    if pd.notna(r.get("pages_expected")):
        pages = f"{int(r['pages_found'])}/{int(r['pages_expected'])}"
        if pd.notna(r.get("best_rank")):
            pages += f" (rank {int(r['best_rank'])})"
    elif pd.notna(r.get("set_recall")):
        pages = f"list P {_pct(r['set_precision'])} / R {_pct(r['set_recall'])}"
    elif pd.notna(r.get("filter_ok")):
        pages = "filter ok" if r["filter_ok"] else "wrong filter"
    cited = f"{int(r['files_cited'])}/{int(r['files_expected'])}" if pd.notna(r.get("files_expected")) else ""
    return {
        "Case": r["id"],
        "Question": r["question"],
        "Correct": (r["correct"] or "ungraded").replace("_", " "),
        "Supported": r["supported"] or "ungraded",
        "Declined": declined,
        "Source found": pages,
        "Cited": cited,
        "Seconds": f"{r['latency_s']:.1f}",
    }


def _headline(o: dict, c: dict, meta: dict) -> str:
    grader = GRADED_BY[c["graded_by"]]
    return (
        f"**Table extraction:** {o['correct']} of {o['labels']} labeled fields correct ({_pct(o['accuracy'])}) "
        f"across {o['documents']} documents; precision {_pct(o['precision'])}, recall {_pct(o['recall'])}; "
        f"invented a value for {o['invented']} of {o['not_stated']} fields the document doesn't state.  \n"
        f"**Chat:** {c['correct']} of {c['answer_cases']} answers correct and {c['partially_correct']} partially "
        f"correct ({grader}); declined {c['correct_declines']} of {c['should_decline']} unanswerable questions but "
        f"also {c['wrong_declines']} of {c['answerable']} answerable ones; expected page found "
        f"{c['pages_found']}/{c['pages_expected']}.  \n"
        f"<sub>Labels verified by a person against the PDFs; chat run fresh {meta['chat_run_at']}. "
        "Details in eval/results/report.md.</sub>"
    )


def build_summary(
    extraction: dict[str, Any],
    extraction_frame: pd.DataFrame,
    chat: dict[str, Any],
    chat_frame: pd.DataFrame,
    notes: dict[str, dict[str, str]],
    meta: dict[str, Any],
) -> dict[str, Any]:
    records = chat_frame.to_dict("records")
    chat_failures = [
        {"item": r["id"], "question": r["question"], "problem": "; ".join(p), **_note(notes, r["id"])}
        for r in records
        if (p := _chat_problems(r))
    ]
    return {
        "meta": meta,
        "extraction": extraction,
        "chat": chat,
        "headline_markdown": _headline(extraction["overall"], chat, meta),
        "extraction_by_field": [
            {k: f[k] for k in ("field", "labels", "accuracy", "invented", "missed", "wrong")}
            for f in extraction["by_field"]
        ],
        "chat_cases": [_cell(r) for r in records],
        "extraction_failures": _extraction_failures(extraction_frame, notes),
        "chat_failures": chat_failures,
    }


def _failure_lines(failures: list[dict[str, str]], label: str) -> list[str]:
    if not failures:
        return ["No failures.", ""]
    lines = []
    for f in failures:
        cause = f" — {f['cause']}" if f["cause"] else " — cause not yet reviewed"
        lines.append(f"- **{f[label]}**: {f['problem']}{cause}")
    return [*lines, ""]


def write_report(summary: dict[str, Any]) -> str:
    meta, ext, c = summary["meta"], summary["extraction"], summary["chat"]
    o, types = ext["overall"], c["by_type"]
    lo, hi = o["accuracy_95ci"]
    agree = c["judge_vs_human"]["correct"]
    by_field = pd.DataFrame(ext["by_field"])[["field", "labels", "accuracy", "invented", "missed", "wrong"]]
    by_field["accuracy"] = by_field["accuracy"].map(_pct)
    by_text = pd.DataFrame(ext["by_text"])[["text", "documents", "labels", "accuracy", "precision", "recall"]]
    for col in ("accuracy", "precision", "recall"):
        by_text[col] = by_text[col].map(_pct)
    ext_failures = [
        f | {"item": f"{_short(f['document'])}, {f['field']}"} for f in summary["extraction_failures"]
    ]
    agreement = (
        f"The AI grader agreed with the person on {agree['agree']} of {agree['compared']} correctness grades."
        if agree["compared"]
        else "No human grades yet, so grader agreement isn't measured."
    )

    lines = [
        "# Evaluation report",
        "",
        f"Chat run {meta['chat_run_at']}; report generated {meta['generated_at']}. Models: extraction "
        f"`{meta['extraction_model']}`, chat `{meta['router_model']}` / `{meta['chat_model']}`, AI grader "
        f"`{meta['judge_model']}` ({meta['judge_prompt_version']}); search `{meta['colbert_model']}`. "
        "Cached model output was not reused.",
        "",
        "## Results",
        "",
        f"- **Table extraction:** {o['correct']} of {o['labels']} field values correct (**{_pct(o['accuracy'])}**, "
        f"95% interval {_pct(lo)}–{_pct(hi)}) across {o['documents']} documents. Of the values the table filled "
        f"in, {_pct(o['precision'])} were right (precision); of the values the documents clearly state, it got "
        f"{_pct(o['recall'])} (recall). It invented a value for **{o['invented']} of {o['not_stated']}** fields "
        f"the document doesn't state. Correct values cite the label's page {_pct(o['page_exact'])} of the time; "
        f"{_pct(o['quote_verified'])} of evidence quotes were found word for word in the page text.",
        f"- **Chat answers:** {c['correct']} of {c['answer_cases']} correct"
        + (f" and {c['partially_correct']} partially correct" if c["partially_correct"] else "")
        + f", {GRADED_BY[c['graded_by']]}. {c['supported']} of {c['supported_of']} answers were fully supported by the "
        f"pages they cite.",
        f"- **Refusals:** declined {c['correct_declines']} of {c['should_decline']} questions the documents can't "
        f"answer, and wrongly declined {c['wrong_declines']} of {c['answerable']} answerable questions.",
        f"- **Finding the source:** the expected page was among the retrieved passages or the evidence quotes "
        f"shown for {c['pages_found']} of {c['pages_expected']} expected pages, and the answer cited the expected "
        f"document {c['files_cited']} of {c['files_expected']} times."
        + (
            f" The list question returned contracts with precision {_pct(c['list_precision'])} and recall "
            f"{_pct(c['list_recall'])}."
            if "list_recall" in c
            else ""
        ),
        f"- **Speed and cost:** median {c['median_latency_s']}s per answer (p90 {c['p90_latency_s']}s), "
        f"${c['mean_cost_usd']:.4f} per question.",
        f"- {agreement}",
        "",
        "## What was tested",
        "",
        f"- **Extraction:** {o['labels']} field labels, 10 decision-critical fields on each of {o['documents']} "
        "documents drawn at random (fixed seed) by document type from files not used in earlier testing, covering "
        "agreements, renewals, an amendment, an award letter, a vendor disclosure, a lease and a task order, "
        "including scanned files. An AI assistant drafted each label with its page and quote; a person checked "
        f"every one against the PDF. {meta['labels_changed_after_results']} labels were changed after results "
        "were seen (logged in the label file).",
        f"- **Chat:** {c['cases']} questions with reference answers: {types.get('structured', 0)} answered from "
        f"the table, {types.get('clause', 0)} clause lookups, {types.get('hybrid', 0)} needing both, and "
        f"{c['should_decline']} the system should decline (a vendor that doesn't exist, a clause the contract "
        "doesn't have, an off-topic question).",
        "",
        "## Table extraction by field",
        "",
        by_field.to_markdown(index=False),
        "",
        by_text.to_markdown(index=False),
        "",
        "Extraction failures:",
        "",
        *_failure_lines(ext_failures, "item"),
        "## Chat cases",
        "",
        pd.DataFrame(summary["chat_cases"]).to_markdown(index=False),
        "",
        "Chat failures:",
        "",
        *_failure_lines(summary["chat_failures"], "item"),
        *(["## Reviewer notes", "", summary["review_notes"], ""] if summary.get("review_notes") else []),
        "## How it is measured, and why",
        "",
        "- **Extraction** is compared by code, not an AI grader, so the score is reproducible: amounts within 0.5%, "
        'dates, day counts, yes/no and document type exactly, vendor names ignoring suffixes like "Inc.". '
        "Precision and recall are separate because the errors cost differently: a wrong value misleads, a blank "
        "only sends the reader to the PDF. The invented-value rate is the hallucination measure. Page accuracy "
        "and verified quotes show whether a value can be checked.",
        "- **Chat** is graded by a person against the reference: *correct* (the key facts) and *supported* (every "
        "claim backed by a cited page; not applicable when it declines), because a right answer with no support "
        "can't be trusted. *Source found / cited* separates search failures from answer-writing failures. "
        "*Refusals* are scored both ways, since a system that refused everything would ace the unanswerable "
        "questions. Latency and cost are reported because a slow or expensive answer fails in use. The route is "
        "logged for diagnosis, not scored.",
        "",
        "## Monitoring in production",
        "",
        "- **Drift signals already logged.** Per answer: route, retrieved pages and scores, refusal flag, latency, "
        "cost. Per extracted value: quote, page, confidence, and whether the quote appears in the page text. "
        "Track weekly: refusal rate, uncited answers, top retrieval score, fill rate and quote-verified rate per "
        "field, OCR confidence. A drop in one field suggests new templates or clause wording; a drop across "
        "fields points to the model or prompt.",
        "- **Release gate and reruns.** Rerun this set fresh on every prompt, model or parser change, and weekly "
        "with no change to catch provider-side drift; block a release that lowers accuracy or refusals or raises "
        "invented values. Prompts are versioned in the database; pin dated model snapshots rather than aliases.",
        "- **Human audit and feedback.** A person reviews a weekly sample, with the AI grader screening more; "
        "grader-human agreement decides how far the grader is trusted. Answers users mark wrong and documents "
        "unlike anything in the test set become new test cases.",
        "",
        "## Limitations",
        "",
        f"- The sample is small: one chat question moves chat accuracy by {round(100 / max(1, c['answer_cases']))} "
        f"points, and the 95% interval on extraction accuracy is {_pct(lo)}–{_pct(hi)}. Read the numbers as a map "
        "of where the pipeline fails, and the harness as something to grow, not as a precise score.",
        "- An AI assistant drafted the labels and a person verified them, so the draft can anchor the reviewer.",
        "- Free-text fields (description, renewal options, liability cap, insurance) are not scored here because "
        "they need judgment; the chat questions cover some of the same clauses.",
        "- Each question was graded on one run, and chat answers change between runs. Running each question "
        "several times and reporting how often the outcome holds would give a steadier chat score.",
        "",
    ]
    return "\n".join(lines)
