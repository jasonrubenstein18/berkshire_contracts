"""Pydantic output contracts for every LLM call (OpenAI strict structured outputs).

Each extracted field is evidence-first: the model writes the verbatim quote and its page before the value,
so values are derived from text rather than recalled, and every value can be verified against the source.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model

Confidence = Literal["high", "medium", "low"]
FieldStatus = Literal["found", "not_found"]

DocumentType = Literal[
    "agreement",
    "amendment_modification",
    "renewal_extension",
    "price_adjustment",
    "award_notice",
    "task_order_sow",
    "vendor_disclosure",
    "solicitation_bid",
    "lease",
    "other",
]
RenewalType = Literal["automatic", "county_option", "mutual_agreement", "vendor_option", "none"]
PricingBasis = Literal[
    "fixed_price",
    "unit_price",
    "hourly_rate",
    "not_to_exceed",
    "subscription_license",
    "percentage_fee",
    "other",
]
Indemnification = Literal["vendor_indemnifies_county", "mutual", "county_indemnifies_vendor"]
AssignmentControl = Literal["consent_required", "prohibited", "permitted", "permitted_with_notice"]

_STRICT = ConfigDict(extra="forbid")


def _field_model(name: str, value_type: Any, value_description: str) -> type[BaseModel]:
    return create_model(
        name,
        __config__=_STRICT,
        status=(
            FieldStatus,
            Field(description="'found' only if the document states this; else 'not_found'."),
        ),
        evidence_quote=(
            str | None,
            Field(
                description="Exact contiguous excerpt (<= 40 words) copied from the text; null if not_found."
            ),
        ),
        evidence_page=(
            int | None,
            Field(description="Page number from the === PAGE n === marker of the quote."),
        ),
        value=(value_type, Field(description=value_description)),
        confidence=(
            Confidence,
            Field(description="high = explicit; medium = needs interpretation; low = ambiguous."),
        ),
    )


TextField = _field_model("TextField", str | None, "Normalized value; null if not_found.")
DateField = _field_model("DateField", str | None, "ISO date YYYY-MM-DD; null if not_found.")
AmountField = _field_model(
    "AmountField", float | None, "Amount in US dollars as a plain number; null if not_found."
)
BoolField = _field_model(
    "BoolField", bool | None, "true/false as defined in the field guide; null if not_found."
)
DaysField = _field_model("DaysField", int | None, "Whole number of days; null if not_found.")
DocumentTypeField = _field_model("DocumentTypeField", DocumentType | None, "Document category.")
RenewalTypeField = _field_model(
    "RenewalTypeField", RenewalType | None, "Renewal mechanism; null if not_found."
)
PricingBasisField = _field_model(
    "PricingBasisField", PricingBasis | None, "Pricing structure; null if not_found."
)
IndemnificationField = _field_model(
    "IndemnificationField", Indemnification | None, "Indemnity direction; null if not_found."
)
AssignmentField = _field_model(
    "AssignmentField", AssignmentControl | None, "Assignment / change-of-control rule; null if not_found."
)


class ContractExtraction(BaseModel):
    model_config = _STRICT

    contract_number: TextField  # type: ignore[valid-type]
    document_type: DocumentTypeField  # type: ignore[valid-type]
    vendor_name: TextField  # type: ignore[valid-type]
    contract_description: TextField  # type: ignore[valid-type]
    effective_date: DateField  # type: ignore[valid-type]
    expiration_date: DateField  # type: ignore[valid-type]
    renewal_type: RenewalTypeField  # type: ignore[valid-type]
    renewal_options: TextField  # type: ignore[valid-type]
    auto_renewal: BoolField  # type: ignore[valid-type]
    renewal_notice_days: DaysField  # type: ignore[valid-type]
    award_amount_usd: AmountField  # type: ignore[valid-type]
    pricing_basis: PricingBasisField  # type: ignore[valid-type]
    termination_for_convenience: BoolField  # type: ignore[valid-type]
    termination_notice_days: DaysField  # type: ignore[valid-type]
    liability_cap: TextField  # type: ignore[valid-type]
    indemnification: IndemnificationField  # type: ignore[valid-type]
    insurance_requirements: TextField  # type: ignore[valid-type]
    governing_law: TextField  # type: ignore[valid-type]
    assignment_change_of_control: AssignmentField  # type: ignore[valid-type]


EXTRACTION_FIELDS: tuple[str, ...] = tuple(ContractExtraction.model_fields)


# ---------------------------------------------------------------------------------------------- chat contracts

FilterField = Literal[
    "document_type",
    "vendor_name",
    "contract_number",
    "family_id",
    "contract_description",
    "effective_date",
    "expiration_date",
    "renewal_type",
    "renewal_options",
    "auto_renewal",
    "renewal_notice_days",
    "award_amount_usd",
    "pricing_basis",
    "termination_for_convenience",
    "termination_notice_days",
    "liability_cap",
    "indemnification",
    "insurance_requirements",
    "governing_law",
    "assignment_change_of_control",
    "needs_review",
    "text_source",
]
FilterOp = Literal[
    "equals",
    "not_equals",
    "contains",
    "greater_than",
    "less_than",
    "between",
    "is_true",
    "is_false",
    "is_known",
    "is_unknown",
    "in_list",
]
GroupField = Literal[
    "document_type",
    "vendor_name",
    "family_id",
    "renewal_type",
    "auto_renewal",
    "pricing_basis",
    "termination_for_convenience",
    "indemnification",
    "governing_law",
    "assignment_change_of_control",
    "expiration_year",
    "expiration_quarter",
    "text_source",
]
SortField = Literal[
    "expiration_date", "effective_date", "award_amount_usd", "vendor_name", "renewal_notice_days"
]
AnswerField = Literal[
    "contract_number",
    "document_type",
    "vendor_name",
    "contract_description",
    "effective_date",
    "expiration_date",
    "renewal_type",
    "renewal_options",
    "auto_renewal",
    "renewal_notice_days",
    "award_amount_usd",
    "pricing_basis",
    "termination_for_convenience",
    "termination_notice_days",
    "liability_cap",
    "indemnification",
    "insurance_requirements",
    "governing_law",
    "assignment_change_of_control",
]


class FilterCondition(BaseModel):
    model_config = _STRICT

    field: FilterField
    op: FilterOp
    value: str | None = Field(
        description="Comparison value as text: dates YYYY-MM-DD, numbers plain, else words."
    )
    value_to: str | None = Field(description="Upper bound for 'between'; otherwise null.")
    values: list[str] = Field(description="Options for 'in_list'; otherwise empty.")


class QueryPlan(BaseModel):
    model_config = _STRICT

    intent: Literal["structured", "clause_search", "hybrid", "out_of_scope"] = Field(
        description=(
            "structured: answerable from extracted fields (filters, counts, totals, charts). "
            "clause_search: needs contract wording. hybrid: filter contracts, then read their clauses. "
            "out_of_scope: unrelated to these contracts."
        )
    )
    filters: list[FilterCondition]
    answer_fields: list[AnswerField] = Field(
        description="Extracted fields whose values answer the question, e.g. ['governing_law']; shown with evidence."
    )
    group_by: GroupField | None
    metric: Literal["list", "count", "sum_award", "avg_award"]
    sort_by: SortField | None
    sort_desc: bool
    limit: int | None
    chart: Literal["none", "bar", "pie", "timeline"]
    search_query: str | None = Field(description="Self-contained retrieval query for clause_search/hybrid.")
    scope_contract_numbers: list[str] = Field(description="Contract numbers the user names, e.g. ['23173'].")
    scope_vendor_names: list[str] = Field(description="Vendor names the user names, as written.")
    explanation: str = Field(description="One sentence restating how the question was interpreted.")


class GroundedAnswer(BaseModel):
    model_config = _STRICT

    answer: str = Field(description="Markdown answer; cite sources inline like [S2]. No uncited claims.")
    cited_sources: list[str] = Field(description="Source ids actually used, e.g. ['S1', 'S3'].")
    insufficient_evidence: bool = Field(description="true if the sources do not answer the question.")
    follow_up_suggestions: list[str] = Field(description="Up to 3 short follow-up questions.")


class JudgeVerdict(BaseModel):
    model_config = _STRICT

    correctness: Literal["correct", "partially_correct", "incorrect"]
    grounded: bool = Field(description="true if every factual claim is supported by the cited source text.")
    abstained: bool = Field(description="true if the answer declined for lack of evidence.")
    rationale: str


class FieldMatch(BaseModel):
    model_config = _STRICT

    field: str
    equivalent: bool
    rationale: str


class FieldMatchBatch(BaseModel):
    model_config = _STRICT

    verdicts: list[FieldMatch]
