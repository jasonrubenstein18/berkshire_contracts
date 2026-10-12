"""Load the test sets in eval/ground_truth. Scoring refuses to run until a person has verified every row."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from berkshire_contracts.config import get_settings
from berkshire_contracts.extraction.fields import FIELD_BY_NAME

NOT_STATED = "not stated"
VERDICTS = {"agree", "corrected"}
EXTRACTION_FILE = "extraction_labels.csv"
CHAT_FILE = "chat_cases.csv"


class UnverifiedLabels(RuntimeError):
    pass


@dataclass(frozen=True)
class FieldLabel:
    file_id: str
    filename: str
    field: str
    value: Any
    accept: tuple[Any, ...]
    page: int | None
    changed_after_results: str


@dataclass(frozen=True)
class ChatCase:
    id: str
    type: str
    question: str
    reference: str
    expected_files: tuple[str, ...]
    optional_files: tuple[str, ...]
    expected_pages: tuple[tuple[str, int], ...]
    should_refuse: bool
    expected_plan: dict | None


def _read(name: str, root: Path | None = None) -> list[dict[str, str]]:
    path = (root or get_settings().ground_truth_dir) / name
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(fh)]


def _require_verified(rows: list[dict[str, str]], name: str) -> None:
    missing = [i for i, r in enumerate(rows, start=2) if r["reviewer_verdict"].lower() not in VERDICTS]
    if missing:
        shown = ", ".join(map(str, missing[:12])) + (" ..." if len(missing) > 12 else "")
        raise UnverifiedLabels(
            f"{name}: {len(missing)} rows have no reviewer_verdict (agree or corrected); lines {shown}"
        )


def _parse_date(raw: str) -> date:
    # Spreadsheet apps rewrite ISO dates as US month/day/year when a reviewer saves the file.
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date {raw!r}; use YYYY-MM-DD")


def parse_value(field: str, raw: str) -> Any:
    raw = raw.strip()
    if not raw or raw.lower() == NOT_STATED:
        return None
    kind = FIELD_BY_NAME[field].kind
    if kind == "date":
        return _parse_date(raw).isoformat()
    if kind == "amount":
        return float(raw.replace(",", "").replace("$", ""))
    if kind == "days":
        return int(raw)
    if kind == "bool":
        if raw.lower() not in {"true", "false"}:
            raise ValueError(f"{field}: expected true or false, got {raw!r}")
        return raw.lower() == "true"
    return raw


def _split(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(";") if part.strip()]


def load_extraction_labels(root: Path | None = None) -> list[FieldLabel]:
    rows = _read(EXTRACTION_FILE, root)
    _require_verified(rows, EXTRACTION_FILE)
    labels = []
    for line, r in enumerate(rows, start=2):
        corrected = r["reviewer_verdict"].lower() == "corrected"
        if corrected and not r["reviewer_value"]:
            raise ValueError(f"{EXTRACTION_FILE} line {line}: 'corrected' needs a reviewer_value")
        labels.append(
            FieldLabel(
                file_id=r["file_id"],
                filename=r["filename"],
                field=r["field"],
                value=parse_value(r["field"], r["reviewer_value"] if corrected else r["value"]),
                accept=tuple(parse_value(r["field"], a) for a in _split(r["accept"])),
                page=int(r["page"]) if r["page"] else None,
                changed_after_results=r["changed_after_results"],
            )
        )
    return labels


def _file_set(name: str, root: Path | None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rows = _read(name, root)
    _require_verified(rows, name)
    member = {r["file_id"]: r["in_set"].lower() for r in rows}
    return (
        tuple(f for f, v in member.items() if v == "yes"),
        tuple(f for f, v in member.items() if v == "optional"),
    )


def load_chat_cases(root: Path | None = None) -> list[ChatCase]:
    rows = _read(CHAT_FILE, root)
    _require_verified(rows, CHAT_FILE)
    cases = []
    for r in rows:
        expected, optional = _split(r["expected_file_ids"]), _split(r["optional_file_ids"])
        if len(expected) == 1 and expected[0].startswith("@"):
            expected, optional = _file_set(expected[0][1:], root)
        pages = []
        for item in _split(r["expected_pages"]):
            file_id, _, page = item.partition(":")
            pages.append((file_id.strip(), int(page)))
        cases.append(
            ChatCase(
                id=r["id"],
                type=r["type"],
                question=r["question"],
                reference=r["reference_answer"],
                expected_files=tuple(expected),
                optional_files=tuple(optional),
                expected_pages=tuple(pages),
                should_refuse=r["should_refuse"].lower() == "true",
                expected_plan=json.loads(r["expected_plan"]) if r["expected_plan"] else None,
            )
        )
    return cases


def labels_changed_after_results(root: Path | None = None) -> int:
    return sum(1 for r in _read(EXTRACTION_FILE, root) if r["changed_after_results"])
