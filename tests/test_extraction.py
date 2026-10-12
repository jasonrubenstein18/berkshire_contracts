import time
from datetime import date

import httpx
import openai
import pytest

from berkshire_contracts.db import Contract
from berkshire_contracts.extraction.extractor import (
    DocumentContext,
    DocumentResult,
    FieldResult,
    consistency_issues,
    flags_for,
    merge_escalation,
    postprocess,
    review_reasons,
)
from berkshire_contracts.extraction.llm import RateLimiter, _retryable
from berkshire_contracts.extraction.normalize import (
    clean_amount,
    clean_days,
    clean_text_value,
    family_from_contract_number,
    norm_text,
    normalize_contract_number,
    parse_iso_date,
    verify_quote,
)
from berkshire_contracts.schemas import EXTRACTION_FIELDS, ContractExtraction

PAGES = {
    1: norm_text("PROFESSIONAL SERVICES AGREEMENT between Lake County and DLR Group, Inc."),
    4: norm_text(
        "This Agreement shall automatically renew for successive one-year terms unless either party gives "
        "sixty (60) days' written notice of non-renewal. The County may terminate for convenience."
    ),
}


def test_verify_quote_exact_and_page_correction():
    check = verify_quote("automatically renew for successive one-year terms", PAGES, claimed_page=1)
    assert check.verified and check.page == 4


def test_verify_quote_tolerates_ellipsis_and_ocr_noise():
    assert verify_quote("shall automatically renew … sixty (60) days' written notice", PAGES, 4).verified
    assert verify_quote(
        "This Agreement shal1 automatica1ly renew for successive one-year terms", PAGES, 4
    ).verified


def test_verify_quote_rejects_invented_text():
    check = verify_quote("Liability is capped at two million dollars per occurrence", PAGES, 4)
    assert not check.verified
    assert not verify_quote(None, PAGES, 1).verified


def test_value_cleaning():
    assert clean_amount("$1,250,000.00") == 1_250_000.0
    assert clean_amount(0) is None and clean_amount(9e12) is None
    assert clean_days(59.6) == 60 and clean_days(0) is None
    assert parse_iso_date("2025-04-04") == date(2025, 4, 4)
    assert parse_iso_date("April 4, 2025") is None and parse_iso_date("2025-02-30") is None
    assert clean_text_value("  Not stated ") is None
    assert normalize_contract_number("Contract No. 23173_2") == "23173-2"
    assert normalize_contract_number("SOW 141301") == "SOW 141301"
    assert family_from_contract_number("23173-2") == "23173"


def field(name, value=None, status=None, verified=True, confidence="high", problem=None):
    status = status or ("found" if value is not None else "not_found")
    return FieldResult(
        name,
        status,
        value,
        None,
        "q" if value is not None else None,
        1,
        confidence,
        verified if value is not None else None,
        100.0,
        problem=problem,
    )


def doc_with(**overrides) -> DocumentResult:
    fields = {name: field(name) for name in EXTRACTION_FIELDS}
    fields.update(overrides)
    return DocumentResult("f1", fields, "model", "v")


def test_consistency_checks():
    issues = consistency_issues(
        doc_with(
            effective_date=field("effective_date", date(2025, 1, 1)),
            expiration_date=field("expiration_date", date(2024, 12, 31)),
            auto_renewal=field("auto_renewal", True),
            renewal_type=field("renewal_type", "county_option"),
        ).fields
    )
    assert "expiration_date precedes effective_date" in issues
    assert any("auto_renewal is true" in i for i in issues)


def test_flags_and_escalation_merge_keep_better_answer():
    first = doc_with(
        vendor_name=field(
            "vendor_name", "DLR Group", verified=False, problem="evidence quote not found in document"
        ),
        liability_cap=field("liability_cap", "$1M", confidence="low"),
    )
    flags = flags_for(first)
    assert set(flags) == {"vendor_name", "liability_cap"}

    second = doc_with(
        vendor_name=field("vendor_name", "DLR Group, Inc."),
        liability_cap=field("liability_cap", "$2M", verified=False),
    )
    merged = merge_escalation(first, second, flags)
    assert merged.fields["vendor_name"].value == "DLR Group, Inc." and merged.fields["vendor_name"].escalated
    assert merged.fields["liability_cap"].value == "$1M"


def extraction_with(**found) -> ContractExtraction:
    data = {
        name: {
            "status": "not_found",
            "evidence_quote": None,
            "evidence_page": None,
            "value": None,
            "confidence": "low",
        }
        for name in EXTRACTION_FIELDS
    }
    for name, (value, quote, page) in found.items():
        data[name] = {
            "status": "found",
            "evidence_quote": quote,
            "evidence_page": page,
            "value": value,
            "confidence": "high",
        }
    return ContractExtraction.model_validate(data)


def context(pages: dict[int, str]) -> DocumentContext:
    return DocumentContext(
        "f1", "f.pdf", len(pages), pages, sorted(pages), False, {p: norm_text(t) for p, t in pages.items()}
    )


def test_documents_titled_as_leases_are_typed_as_leases():
    ctx = context(
        {
            1: 'THIS WIRELESS COMMUNICATIONS SITE LEASE ("Lease") is made as of July 1, 2016.',
            2: "MASTER SERVICES AGREEMENT between Lake County and Acme, Inc.",
        }
    )
    titled = extraction_with(document_type=("agreement", "THIS WIRELESS COMMUNICATIONS SITE LEASE", 1))
    assert postprocess(titled, ctx, "m", "v").fields["document_type"].value == "lease"
    plain = extraction_with(document_type=("agreement", "MASTER SERVICES AGREEMENT", 2))
    assert postprocess(plain, ctx, "m", "v").fields["document_type"].value == "agreement"


def test_explicit_auto_renew_wording_escalates_a_false_auto_renewal():
    ctx = context(
        {
            1: "The County may renew this Agreement by mutual written agreement.",
            97: "Upon completion of the Subscription Term, the Subscription Term will automatically renew for "
            "an additional 5 years.",
        }
    )
    first = postprocess(
        extraction_with(
            auto_renewal=(False, "The County may renew this Agreement by mutual written agreement", 1)
        ),
        ctx,
        "m",
        "v",
    )
    flags = flags_for(first)
    assert {"auto_renewal", "renewal_type"} <= set(flags) and "p.97" in flags["auto_renewal"]

    second = postprocess(
        extraction_with(auto_renewal=(True, "the Subscription Term will automatically renew", 97)),
        ctx,
        "m",
        "v",
    )
    merged = merge_escalation(first, second, flags, ctx)
    assert merged.fields["auto_renewal"].value is True and not merged.consistency_issues

    loose = context({10: "Automatic renewal is subject to approval by the County Board."})
    doc = postprocess(
        extraction_with(auto_renewal=(False, "Automatic renewal is subject to approval", 10)), loose, "m", "v"
    )
    assert "auto_renewal" not in flags_for(doc)


def test_review_reasons_flag_contract_number_mismatch():
    doc = doc_with(contract_number=field("contract_number", "20027"))
    contract = Contract(file_id="f1", family_id="22027", parse_status="partial", mean_ocr_confidence=55.0)
    reasons = review_reasons(doc, contract)
    assert any("differs from filename" in r for r in reasons)
    assert "some pages had no extractable text" in reasons
    assert any("low OCR confidence" in r for r in reasons)


def _rate_limit_error(message: str) -> openai.RateLimitError:
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    return openai.RateLimitError(message, response=response, body=None)


def test_retry_policy_skips_daily_quota_errors():
    assert _retryable(_rate_limit_error("Rate limit reached for requests per min (RPM)"))
    assert not _retryable(_rate_limit_error("Rate limit reached for requests per day (RPD)"))
    assert not _retryable(_rate_limit_error("You exceeded your current quota: insufficient_quota"))


class FakeClockCondition:
    def __init__(self, clock):
        self.clock = clock

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def wait(self, timeout=None):
        self.clock["t"] += timeout or 0
        return False


@pytest.mark.parametrize(("rpm", "tpm", "tokens"), [(2, 1_000_000, 10), (100, 1_000, 600)])
def test_rate_limiter_waits_for_the_window(monkeypatch, rpm, tpm, tokens):
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    limiter = RateLimiter(rpm=rpm, tpm=tpm)
    limiter._cond = FakeClockCondition(clock)
    limiter.acquire(tokens)
    if rpm == 2:
        limiter.acquire(tokens)
    start = clock["t"]
    waited = limiter.acquire(tokens)
    assert clock["t"] - start >= 59 and waited >= 59


@pytest.mark.parametrize("value", [None, ""])
def test_contract_number_normalization_handles_empty(value):
    assert normalize_contract_number(value) is None
