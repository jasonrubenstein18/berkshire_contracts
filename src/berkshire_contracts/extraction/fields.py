"""The 19 extracted features: how to extract each one, and the business decision it supports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: str  # text | date | amount | bool | days | enum
    group: str
    instruction: str
    business_use: str
    retrieval_query: str


FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "contract_number",
        "Contract #",
        "text",
        "Identity",
        "The County contract, bid, or RFP number this document belongs to, e.g. '23173' or '23173-2' when a "
        "multi-award contract has numbered sub-awards. Prefer the County contract number over purchase-order, "
        "board-resolution, or vendor quote numbers.",
        "Joins letters, amendments and agreements into one contract family and lifecycle.",
        "contract number bid number RFP number agreement number",
    ),
    FieldSpec(
        "document_type",
        "Document type",
        "enum",
        "Identity",
        "agreement = executed contract, services agreement, license, MOU or lease-like master terms; "
        "amendment_modification = changes an existing contract (modification, amendment, letter of agreement); "
        "renewal_extension = renews or extends a term; price_adjustment = changes rates or prices only; "
        "award_notice = award or intent-to-award letter or award summary; task_order_sow = task order or "
        "statement of work under a master agreement; vendor_disclosure = vendor disclosure / ethics statement; "
        "solicitation_bid = RFP, bid document, bid tabulation, quote or proposal; lease = equipment or property "
        "lease; other = anything else. Always 'found'.",
        "Separates source-of-truth agreements from correspondence; tells reviewers which fields should exist.",
        "agreement amendment renewal award letter disclosure statement",
    ),
    FieldSpec(
        "vendor_name",
        "Vendor",
        "text",
        "Identity",
        "Legal name of the counterparty (vendor, contractor, consultant, or lessor). Never Lake County itself.",
        "Vendor concentration and spend-by-supplier analysis.",
        "vendor contractor consultant company name between Lake County and",
    ),
    FieldSpec(
        "contract_description",
        "Description",
        "text",
        "Identity",
        "One plain-English sentence (<= 25 words) describing the goods or services. Quote the passage that "
        "names them.",
        "Categorizes spend so contracts can be benchmarked and consolidated.",
        "scope of services description of work goods services provided",
    ),
    FieldSpec(
        "effective_date",
        "Effective date",
        "date",
        "Term",
        "Start date of the agreement, or of the term this document covers (for a renewal letter, the start of "
        "the renewal period). If only month and year are given use the first day of that month. If the "
        "document is effective upon execution/signature, use the latest dated signature (quote that dated "
        "signature line) and set confidence to medium.",
        "Term tracking and contract age.",
        "effective date commencement term begins start date",
    ),
    FieldSpec(
        "expiration_date",
        "Expiration date",
        "date",
        "Term",
        "End date of the current term this document establishes or covers (for a renewal letter, the end of the "
        "renewal period). If only month and year are given use the last day of that month. If only a duration "
        "is stated (e.g. 'a one-year period') and the effective date is known, compute effective date + "
        "duration - 1 day, quote the duration clause, and set confidence to medium.",
        "Builds the renewal calendar: which contracts must be renegotiated or rebid, and when.",
        "term expires expiration date end date through terminate on",
    ),
    FieldSpec(
        "renewal_type",
        "Renewal type",
        "enum",
        "Renewal",
        "Who controls renewal: automatic = renews unless someone gives notice; county_option = the County may "
        "renew or extend at its option; mutual_agreement = both parties must agree in writing; vendor_option = "
        "the vendor controls renewal; none = the document expressly provides no renewal.",
        "Shows negotiating leverage: options the County holds versus terms that roll over by default.",
        "renewal option renew extend additional terms periods",
    ),
    FieldSpec(
        "renewal_options",
        "Renewal options",
        "text",
        "Renewal",
        "The renewal terms in a short phrase, e.g. 'Up to two additional one-year renewals' or 'Renewal 2 of 4: "
        "6/1/2025-5/31/2026'.",
        "Runway before a contract must be rebid.",
        "renewal option additional one-year periods extension remaining",
    ),
    FieldSpec(
        "auto_renewal",
        "Auto-renewal",
        "bool",
        "Renewal",
        "true if the term renews automatically without any action by the parties (evergreen); false if renewal "
        "requires an affirmative act such as written agreement or County option; not_found if renewal is not "
        "addressed.",
        "Flags evergreen terms that renew silently unless someone acts before the notice deadline.",
        "automatically renew successive terms unless written notice of non-renewal",
    ),
    FieldSpec(
        "renewal_notice_days",
        "Renewal notice (days)",
        "days",
        "Renewal",
        "Days of notice required to renew, exercise a renewal option, or prevent automatic renewal. Convert "
        "months to days (1 month = 30 days).",
        "The action deadline that drives the renewal calendar.",
        "days prior written notice renewal non-renewal intent to renew",
    ),
    FieldSpec(
        "award_amount_usd",
        "Award amount (USD)",
        "amount",
        "Economics",
        "Total contract value, not-to-exceed amount, or annual award amount stated for this document's term, in "
        "US dollars. Do not add up unit prices or line items yourself; use a stated total.",
        "Spend under management and prioritization of high-value renegotiations.",
        "total contract amount not to exceed compensation award amount price",
    ),
    FieldSpec(
        "pricing_basis",
        "Pricing basis",
        "enum",
        "Economics",
        "fixed_price = lump sum; unit_price = per-unit or per-item rates; hourly_rate = time and materials or "
        "hourly rates; not_to_exceed = capped amount billed as used; subscription_license = recurring license or "
        "subscription fees; percentage_fee = percentage-based fees; other = anything else.",
        "Indicates cost variability and how invoices should be audited.",
        "pricing rates fees unit price hourly lump sum compensation schedule",
    ),
    FieldSpec(
        "termination_for_convenience",
        "Termination for convenience",
        "bool",
        "Risk",
        "true if the County (or either party) may terminate without cause or for convenience; false only if the "
        "document expressly limits termination to cause; not_found otherwise.",
        "Exit flexibility: whether a contract can be ended early without paying for breach.",
        "terminate for convenience without cause written notice",
    ),
    FieldSpec(
        "termination_notice_days",
        "Termination notice (days)",
        "days",
        "Risk",
        "Days of written notice required to terminate for convenience; if there is no convenience right, the "
        "notice or cure period for termination for cause. Convert months to days.",
        "Time needed to exit or transition a vendor.",
        "days written notice termination cure period",
    ),
    FieldSpec(
        "liability_cap",
        "Liability cap",
        "text",
        "Risk",
        "The limitation-of-liability terms in a short phrase, e.g. 'Vendor liability capped at fees paid in the "
        "prior 12 months; excludes indemnity'. not_found if there is no limitation of liability clause.",
        "Sizes the County's exposure if a vendor fails.",
        "limitation of liability shall not exceed liable damages cap consequential",
    ),
    FieldSpec(
        "indemnification",
        "Indemnification",
        "enum",
        "Risk",
        "Direction of indemnity: vendor_indemnifies_county, mutual, or county_indemnifies_vendor.",
        "Shows who carries third-party claim risk.",
        "indemnify defend hold harmless claims losses",
    ),
    FieldSpec(
        "insurance_requirements",
        "Insurance requirements",
        "text",
        "Risk",
        "Required insurance types and limits in a short phrase, e.g. 'CGL $1M per occurrence / $2M aggregate; "
        "auto $1M; workers comp statutory; umbrella $5M'.",
        "Certificate-of-insurance compliance and risk transfer.",
        "insurance general liability per occurrence aggregate workers compensation certificate",
    ),
    FieldSpec(
        "governing_law",
        "Governing law",
        "text",
        "Risk",
        "The state whose law governs the contract, e.g. 'Illinois'. Venue alone is not governing law unless "
        "stated together.",
        "Legal risk and standardization of terms.",
        "governed by the laws of the State of Illinois venue jurisdiction",
    ),
    FieldSpec(
        "assignment_change_of_control",
        "Assignment / change of control",
        "enum",
        "Risk",
        "consent_required = assignment or change of control needs the other party's consent; prohibited = not "
        "allowed; permitted = freely assignable; permitted_with_notice = allowed with notice only.",
        "Change-of-control risk if a vendor is acquired, which is central in M&A diligence.",
        "assign transfer subcontract consent change of control",
    ),
)

FIELD_BY_NAME: dict[str, FieldSpec] = {spec.name: spec for spec in FIELD_SPECS}

ENUM_LABELS: dict[str, str] = {
    "agreement": "Agreement",
    "amendment_modification": "Amendment / modification",
    "renewal_extension": "Renewal / extension",
    "price_adjustment": "Price adjustment",
    "award_notice": "Award notice",
    "task_order_sow": "Task order / SOW",
    "vendor_disclosure": "Vendor disclosure",
    "solicitation_bid": "Solicitation / bid",
    "lease": "Lease",
    "other": "Other",
    "automatic": "Automatic",
    "county_option": "County option",
    "mutual_agreement": "Mutual agreement",
    "vendor_option": "Vendor option",
    "none": "No renewal",
    "fixed_price": "Fixed price",
    "unit_price": "Unit price",
    "hourly_rate": "Hourly rate",
    "not_to_exceed": "Not to exceed",
    "subscription_license": "Subscription / license",
    "percentage_fee": "Percentage fee",
    "vendor_indemnifies_county": "Vendor indemnifies County",
    "mutual": "Mutual",
    "county_indemnifies_vendor": "County indemnifies vendor",
    "consent_required": "Consent required",
    "prohibited": "Prohibited",
    "permitted": "Permitted",
    "permitted_with_notice": "Permitted with notice",
}


def field_guide() -> str:
    lines = ["FIELD GUIDE"]
    for i, spec in enumerate(FIELD_SPECS, start=1):
        lines.append(f"{i}. {spec.name} ({spec.kind}): {spec.instruction}")
    return "\n".join(lines)
