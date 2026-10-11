"""Value normalization and evidence verification (is the quoted text really on the cited page?)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from rapidfuzz import fuzz

_NON_ALNUM = re.compile(r"[^0-9a-z$%]+")
_ELLIPSIS = re.compile(r"\s*(?:\.\.\.|…)\s*")
EXACT_MATCH_MIN_CHARS = 12
FUZZY_THRESHOLD = 88.0


def norm_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return " ".join(_NON_ALNUM.sub(" ", text).split())


@dataclass
class QuoteCheck:
    verified: bool
    page: int | None
    score: float


def _fragment_score(fragment: str, page_norm: str) -> float:
    if not fragment:
        return 0.0
    if fragment in page_norm:
        return 100.0
    if len(fragment) < EXACT_MATCH_MIN_CHARS:
        return 0.0
    return float(fuzz.partial_ratio(fragment, page_norm))


def _quote_score(quote_norm_fragments: list[str], page_norm: str) -> float:
    scores = [_fragment_score(f, page_norm) for f in quote_norm_fragments]
    weights = [len(f) for f in quote_norm_fragments]
    return sum(s * w for s, w in zip(scores, weights, strict=True)) / max(1, sum(weights))


def verify_quote(quote: str | None, normalized_pages: dict[int, str], claimed_page: int | None) -> QuoteCheck:
    """Accept exact (normalized) or near-exact matches; OCR noise makes strict equality too brittle."""
    if not quote:
        return QuoteCheck(False, None, 0.0)
    fragments = [norm_text(f) for f in _ELLIPSIS.split(quote) if norm_text(f)]
    if not fragments:
        return QuoteCheck(False, None, 0.0)
    order = list(normalized_pages)
    if claimed_page in normalized_pages:
        order.remove(claimed_page)
        order.insert(0, claimed_page)
    best_page, best_score = None, 0.0
    for page_no in order:
        score = _quote_score(fragments, normalized_pages[page_no])
        if score > best_score:
            best_page, best_score = page_no, score
        if score >= 99.5:
            break
    verified = best_score >= FUZZY_THRESHOLD
    return QuoteCheck(verified, best_page if verified else claimed_page, round(best_score, 1))


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", value)
    if not match:
        return None
    year, month, day = (int(g) for g in match.groups())
    if not 1980 <= year <= 2060:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def clean_amount(value: float | int | str | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.\-]", "", value)
        if not cleaned:
            return None
        value = float(cleaned)
    amount = float(value)
    if amount <= 0 or amount > 5e9:
        return None
    return round(amount, 2)


def clean_days(value: int | float | None) -> int | None:
    if value is None:
        return None
    days = round(float(value))
    return days if 0 < days <= 3650 else None


def clean_text_value(value: str | None, max_len: int = 400) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).split())
    if not value or value.lower() in {"n/a", "na", "none", "null", "not stated", "not found", "unknown"}:
        return None
    return value[:max_len]


def normalize_contract_number(value: str | None) -> str | None:
    value = clean_text_value(value, 64)
    if value is None:
        return None
    match = re.search(r"(?<!\d)(\d{5})(?:\s*[-_]\s*(\d))?(?!\d)", value)
    if match and 14000 <= int(match.group(1)) <= 26999:
        return f"{match.group(1)}-{match.group(2)}" if match.group(2) else match.group(1)
    return value


def family_from_contract_number(value: str | None) -> str | None:
    if not value:
        return None
    match = re.match(r"^(\d{5})(?:-\d)?$", value)
    return match.group(1) if match else None
