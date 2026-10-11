"""Field comparators for scoring extracted values against hand labels."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from rapidfuzz import fuzz

from berkshire_contracts.extraction.fields import FIELD_BY_NAME

JUDGED_TEXT_FIELDS = {"contract_description", "renewal_options", "liability_cap", "insurance_requirements"}
_COMPANY_SUFFIXES = {"inc", "llc", "corp", "corporation", "company", "co", "ltd", "the", "incorporated", "lp"}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _vendor_norm(text: str) -> str:
    return " ".join(t for t in _norm(text).split() if t not in _COMPANY_SUFFIXES)


def to_comparable(field: str, value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    kind = FIELD_BY_NAME[field].kind
    if kind == "amount":
        return float(value)
    if kind == "days":
        return int(value)
    if kind == "bool":
        return bool(value)
    return str(value)


def deterministic_match(field: str, predicted: Any, expected: Any) -> bool | None:
    """True/False for fields with an objective comparison; None when a semantic judgment is needed."""
    if predicted is None or expected is None:
        return predicted is None and expected is None
    kind = FIELD_BY_NAME[field].kind
    if kind == "amount":
        return abs(float(predicted) - float(expected)) <= max(1.0, 0.005 * abs(float(expected)))
    if kind in {"days", "bool", "date", "enum"}:
        return predicted == expected
    if field == "contract_number":
        p, e = (
            re.sub(r"[^a-z0-9]", "", str(predicted).lower()),
            re.sub(r"[^a-z0-9]", "", str(expected).lower()),
        )
        return p == e or (len(e) >= 5 and e in p)
    if field == "vendor_name":
        p, e = _vendor_norm(str(predicted)), _vendor_norm(str(expected))
        return p == e or fuzz.token_set_ratio(p, e) >= 88 or (len(e) >= 4 and (e in p or p in e))
    if field == "governing_law":
        p, e = _norm(str(predicted)), _norm(str(expected))
        return e in p or p in e
    if field in JUDGED_TEXT_FIELDS:
        return None
    return _norm(str(predicted)) == _norm(str(expected))


def fuzzy_text_match(predicted: str, expected: str) -> bool:
    return fuzz.token_set_ratio(_norm(predicted), _norm(expected)) >= 75
