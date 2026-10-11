"""Turns a user question into a validated QueryPlan (structured filters and/or a clause-search query)."""

from __future__ import annotations

import logging
from datetime import date

from berkshire_contracts.config import get_settings
from berkshire_contracts.extraction.fields import FIELD_SPECS
from berkshire_contracts.extraction.llm import CallStats, LlmClient
from berkshire_contracts.schemas import QueryPlan

log = logging.getLogger(__name__)

ROUTER_PROMPT_VERSION = "router-v2"

_FIELD_LINES = "\n".join(
    f"- {s.name} ({s.kind}): {s.label}. {s.instruction.split('.')[0]}." for s in FIELD_SPECS
)

ROUTER_INSTRUCTIONS = f"""You plan how to answer questions about a database of Lake County, Illinois procurement
documents (agreements, amendments, renewal letters, award letters, task orders, leases, vendor disclosures).
Each row is one document with these extracted fields:
{_FIELD_LINES}
- needs_review (bool): the extraction was flagged for human review.
- text_source (enum native/ocr/mixed): how the text was obtained.
- family_id (text): 5-digit contract family that ties letters, amendments and agreements together.

Enum values: document_type = agreement, amendment_modification, renewal_extension, price_adjustment, award_notice,
task_order_sow, vendor_disclosure, solicitation_bid, lease, other. renewal_type = automatic, county_option,
mutual_agreement, vendor_option, none. pricing_basis = fixed_price, unit_price, hourly_rate, not_to_exceed,
subscription_license, percentage_fee, other. indemnification = vendor_indemnifies_county, mutual,
county_indemnifies_vendor. assignment_change_of_control = consent_required, prohibited, permitted,
permitted_with_notice.

Choose intent:
- structured: the answer comes from the fields above (lists, counts, totals, averages, breakdowns, charts).
- clause_search: the user needs what the contract text says (wording, conditions, specific terms, explanations).
- hybrid: first narrow documents with filters, then read their clauses (e.g. "for leases, what happens to the
  equipment when the lease ends?").
- out_of_scope: not about these contracts.

Rules:
- Only use the listed fields and operators. Booleans use is_true / is_false. Dates are YYYY-MM-DD; today is
  {{today}}. "Expiring next year" means between Jan 1 and Dec 31 of next year.
- Put contract numbers the user mentions in scope_contract_numbers and vendor or product names in
  scope_vendor_names when the question is about specific contracts; do not repeat them as filters.
- answer_fields lists the extracted fields whose values answer the question (e.g. insurance_requirements for
  "what insurance is required"); they are shown with their evidence quote. Leave it empty when the question
  only asks which documents match or how many.
- For clause_search and hybrid, write search_query as a self-contained description of the clause wording to
  find, e.g. "vendor may not subcontract without written approval". For structured questions, set
  search_query to null.
- Use metric "list" to show documents; "count" for how many; "sum_award"/"avg_award" for dollar totals.
- Use group_by plus chart "bar" or "pie" for breakdowns, and chart "timeline" for expiration schedules.
- Questions about the whole corpus default to all document types; filter document_type only when the user
  asks for a specific kind of document.

Examples:
Q: Which documents are flagged for review? -> structured; filters [needs_review is_true]; metric list.
Q: How many contracts expire in 2028, by quarter? -> structured; filters [expiration_date between 2028-01-01
and 2028-12-31]; group_by expiration_quarter; metric count; chart bar.
Q: Total award value by vendor -> structured; group_by vendor_name; metric sum_award; chart bar; limit 15.
Q: What insurance does the Acme janitorial agreement require? -> structured; scope_vendor_names ["Acme"];
answer_fields [insurance_requirements]; metric list.
Q: Which agreements need County consent to assign, and how is liability capped? -> structured; filters
[assignment_change_of_control equals consent_required, document_type equals agreement]; answer_fields
[liability_cap]; metric list.
Q: What does the Acme lease say about ending early? -> clause_search; scope_vendor_names ["Acme"];
search_query "tenant right to terminate the lease before the end of the term notice".
Q: For leases, what happens to the equipment when the lease ends? -> hybrid; filters [document_type equals
lease]; search_query "return or removal of equipment at expiration of the lease".
"""


def fallback_plan(question: str, reason: str) -> QueryPlan:
    return QueryPlan(
        intent="clause_search",
        filters=[],
        answer_fields=[],
        group_by=None,
        metric="list",
        sort_by=None,
        sort_desc=False,
        limit=None,
        chart="none",
        search_query=question,
        scope_contract_numbers=[],
        scope_vendor_names=[],
        explanation=f"Searched contract text directly ({reason}).",
    )


def plan_question(
    question: str, client: LlmClient, budget_usd: float | None = None
) -> tuple[QueryPlan, CallStats]:
    settings = get_settings()
    plan, stats = client.parse(
        purpose="route",
        model=settings.router_model,
        instructions=ROUTER_INSTRUCTIONS.replace("{today}", date.today().isoformat()),
        user_input=question,
        schema=QueryPlan,
        prompt_version=ROUTER_PROMPT_VERSION,
        reasoning_effort=settings.router_reasoning_effort,
        max_output_tokens=4000,
        budget_usd=budget_usd,
    )
    if plan.intent in {"clause_search", "hybrid"} and not (plan.search_query or "").strip():
        plan = plan.model_copy(update={"search_query": question})
    return plan, stats
