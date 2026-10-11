"""Filename heuristics. They only drive sampling and family grouping; the LLM reads the actual document type."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

_FIVE_DIGIT = re.compile(r"(?<!\d)(\d{5})(?!\d)")
_RESOLUTION = re.compile(r"^(\d{2})_(\d{4})(?:_|\.|$)")
# A single digit after the contract number is a sub-award (`23173_2`) unless it starts a date (`_9_26_19`).
_SUB_AWARD = re.compile(r"^_(\d)(?!_\d{1,2}_\d{2,4}(?:_|$))(?=_|$)")

# Ordered: the first matching rule wins, so specific document kinds precede generic "agreement".
DOC_TYPE_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("vendor_disclosure", ("vendor_disclosure", "vendor_diclosure", "_vds", "vds.")),
    (
        "price_adjustment",
        ("price_increase", "rate_adjustment", "rate_alignment", "price_proposal_correction"),
    ),
    ("award_notice", ("intent_to_award", "award_letter", "award_information", "_award.", "award_")),
    ("renewal_extension", ("renewal", "extension", "_renew")),
    ("amendment_modification", ("modification", "amendment", "_loa", "loa_", "assignment_of")),
    ("task_order_sow", ("task_order", "_sow", "sow_")),
    ("solicitation_bid", ("bid_doc", "bid_tab", "rfp_", "_proposal", "pricing", "_quote")),
    ("lease", ("lease",)),
    ("agreement", ("agreement", "contract", "executed", "_psa", "license", "signed", "mou", "agmt")),
]


@dataclass(frozen=True)
class FilenameFacts:
    heuristic_doc_type: str
    family_id: str | None
    sub_contract_id: str | None
    related_contracts: list[str] = field(default_factory=list)


def file_id_for(member_name: str) -> str:
    return hashlib.sha1(member_name.encode("utf-8")).hexdigest()[:12]


def heuristic_doc_type(filename: str) -> str:
    name = filename.lower()
    for doc_type, needles in DOC_TYPE_RULES:
        if any(needle in name for needle in needles):
            return doc_type
    return "other"


def _is_year_with_copy_suffix(stem: str, start: int, token: str) -> bool:
    """`2025_20261` is the year 2026 plus a duplicate-copy suffix, not contract 20261."""
    if not token.startswith("20"):
        return False
    preceding = stem[max(0, start - 5) : start]
    return bool(re.fullmatch(r"20\d\d_", preceding)) and int(token[:4]) >= int(preceding[:4])


def contract_numbers(filename: str) -> list[str]:
    """County contract numbers in a filename, with sub-award suffixes (`23173_2` -> `23173-2`)."""
    stem = filename.rsplit(".", 1)[0]
    found: list[str] = []
    for match in _FIVE_DIGIT.finditer(stem):
        token = match.group(1)
        if _is_year_with_copy_suffix(stem, match.start(), token):
            continue
        if not 14000 <= int(token) <= 26999:
            continue
        sub = _SUB_AWARD.match(stem[match.end() :])
        number = f"{token}-{sub.group(1)}" if sub else token
        if number not in found:
            found.append(number)
    return found


def parse_filename(filename: str) -> FilenameFacts:
    numbers = contract_numbers(filename)
    family_id: str | None = None
    sub_id: str | None = None
    if numbers:
        sub_id = numbers[0]
        family_id = sub_id.split("-")[0]
    else:
        resolution = _RESOLUTION.match(filename)
        if resolution:
            family_id = f"{resolution.group(1)}-{resolution.group(2)}"
            sub_id = family_id
    return FilenameFacts(
        heuristic_doc_type=heuristic_doc_type(filename),
        family_id=family_id,
        sub_contract_id=sub_id,
        related_contracts=numbers[1:],
    )
