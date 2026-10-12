"""Deterministic execution of a validated QueryPlan over the contracts table (no model-written SQL)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from rapidfuzz import fuzz
from sqlalchemy import select

from berkshire_contracts.db import Contract, FieldEvidence, Page, get_engine
from berkshire_contracts.extraction.fields import ENUM_LABELS, FIELD_BY_NAME
from berkshire_contracts.schemas import FilterCondition, QueryPlan

DATE_FIELDS = {"effective_date", "expiration_date"}
NUMERIC_FIELDS = {"award_amount_usd", "renewal_notice_days", "termination_notice_days"}
BOOL_FIELDS = {"auto_renewal", "termination_for_convenience", "needs_review"}
DISPLAY_COLUMNS = [
    "filename",
    "contract_number",
    "document_type",
    "vendor_name",
    "effective_date",
    "expiration_date",
    "award_amount_usd",
]
MAX_ROWS = 200

FIELD_LABELS = {name: spec.label for name, spec in FIELD_BY_NAME.items()} | {
    "filename": "File",
    "needs_review": "Needs review",
    "text_source": "Text source",
    "family_id": "Contract family",
    "expiration_year": "Expiration year",
    "expiration_quarter": "Expiration quarter",
}


@dataclass
class StructuredResult:
    table: pd.DataFrame
    summary: str
    matched: int
    file_ids: list[str]
    chart: go.Figure | None = None
    notes: list[str] = field(default_factory=list)


_VENDOR_NOISE = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "corp",
    "corporation",
    "co",
    "company",
    "lp",
    "llp",
    "pc",
    "plc",
    "the",
    "us",
    "usa",
}


def vendor_key(name: str | None) -> str | None:
    if name is None or pd.isna(name) or not str(name).strip():
        return None
    text = re.sub(r"\bd\s*/?\s*b\s*/?\s*a\b.*$", "", str(name).lower())
    tokens = re.sub(r"[^a-z0-9& ]", " ", text).split()
    tokens = [t[:-1] if len(t) > 3 and t.endswith("s") else t for t in tokens if t not in _VENDOR_NOISE]
    return " ".join(tokens) or None


def vendor_groups(names: pd.Series) -> pd.Series:
    """One display name per vendor across spelling variants ("& Sons, Inc." vs "& Son, Inc.")."""
    keys = names.map(vendor_key)
    clusters: list[tuple[str, list[str]]] = []
    cluster_of: dict[str, int] = {}
    for key, original in zip(keys, names, strict=True):
        if pd.isna(key):
            continue
        if key not in cluster_of:
            match = next(
                (i for i, (k, _) in enumerate(clusters) if fuzz.token_sort_ratio(key, k) >= 92), None
            )
            if match is None:
                clusters.append((key, []))
                match = len(clusters) - 1
            cluster_of[key] = match
        clusters[cluster_of[key]][1].append(str(original).strip())
    labels = [max(set(m), key=lambda n, m=m: (m.count(n), len(n))) for _, m in clusters]
    return keys.map(lambda k: None if pd.isna(k) else labels[cluster_of[k]])


def dedupe_awards(df: pd.DataFrame) -> pd.DataFrame:
    """One row per contract family and vendor, keeping the largest stated award.

    Award letters, price-correction LOAs, agreements and task orders often restate (or draw down) the same award,
    so summing award amounts across files double-counts.
    """
    amounts = pd.to_numeric(df["award_amount_usd"], errors="coerce")
    d = df.assign(award_amount_usd=amounts).dropna(subset=["award_amount_usd"])
    return d.sort_values("award_amount_usd", ascending=False).drop_duplicates("award_group")


def load_contracts_frame(include_duplicates: bool = False) -> pd.DataFrame:
    with get_engine().connect() as conn:
        df = pd.read_sql(select(Contract.__table__), conn)
    if not include_duplicates:
        df = df[df["duplicate_of"].isna()]
    for col in DATE_FIELDS:
        df[col] = pd.to_datetime(df[col], errors="coerce")
    df = df.reset_index(drop=True)
    df["vendor_group"] = vendor_groups(df["vendor_name"])
    df["award_group"] = df["family_id"].fillna(df["file_id"]) + "|" + df["vendor_group"].fillna(df["file_id"])
    return df


def load_evidence(
    file_ids: list[str], fields: list[str]
) -> dict[tuple[str, str], tuple[str | None, int | None]]:
    if not file_ids or not fields:
        return {}
    with get_engine().connect() as conn:
        rows = conn.execute(
            select(
                FieldEvidence.file_id, FieldEvidence.field_name, FieldEvidence.quote, FieldEvidence.page
            ).where(FieldEvidence.file_id.in_(file_ids), FieldEvidence.field_name.in_(fields))
        ).all()
    return {(r.file_id, r.field_name): (r.quote, r.page) for r in rows}


def _as_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    v = value.strip().lower()
    if v in {"true", "yes", "1", "y"}:
        return True
    if v in {"false", "no", "0", "n"}:
        return False
    return None


def _as_number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def _as_date(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(parsed) else parsed


def _text_match(series: pd.Series, value: str, exact: bool) -> pd.Series:
    target = value.strip().lower().replace(" ", "_") if exact else value.strip().lower()
    lowered = series.fillna("").astype(str).str.lower()
    if exact:
        return (
            (lowered == target)
            | (lowered.str.replace(" ", "_") == target)
            | (lowered == value.strip().lower())
        )
    return lowered.str.contains(target, regex=False)


def apply_filter(df: pd.DataFrame, cond: FilterCondition) -> tuple[pd.DataFrame, str | None]:
    """Returns the filtered frame and a note if the condition could not be applied as written."""
    col, op = cond.field, cond.op
    if col not in df.columns:
        return df, f"ignored filter on unknown field {col}"
    series = df[col]

    if op == "is_known":
        return df[series.notna()], None
    if op == "is_unknown":
        return df[series.isna()], None

    if col in BOOL_FIELDS:
        target = {"is_true": True, "is_false": False}.get(op)
        if target is None and op in {"equals", "not_equals"}:
            target = _as_bool(cond.value)
            if target is not None and op == "not_equals":
                target = not target
        if target is None:
            return df, f"ignored filter {col} {op} {cond.value!r}: not a yes/no condition"
        return df[series.astype("boolean").fillna(not target) == target], None

    if col in DATE_FIELDS or col in NUMERIC_FIELDS:
        parse = _as_date if col in DATE_FIELDS else _as_number
        low, high = parse(cond.value), parse(cond.value_to)
        values = series if col in DATE_FIELDS else pd.to_numeric(series, errors="coerce")
        if op == "greater_than" and low is not None:
            return df[values > low], None
        if op == "less_than" and low is not None:
            return df[values < low], None
        if op == "between" and low is not None and high is not None:
            return df[(values >= low) & (values <= high)], None
        if op == "equals" and low is not None:
            return df[values == low], None
        if op == "not_equals" and low is not None:
            return df[values != low], None
        return (
            df,
            f"ignored filter {col} {op} {cond.value!r}: needs a valid {'date' if col in DATE_FIELDS else 'number'}",
        )

    if op == "in_list" and cond.values:
        mask = pd.Series(False, index=df.index)
        for v in cond.values:
            mask |= _text_match(series, v, exact=True)
        return df[mask], None
    if op in {"equals", "not_equals", "contains"} and cond.value:
        if col == "vendor_name":
            mask = _name_mask(df, cond.value, fuzzy=op != "contains")
        else:
            mask = _text_match(series, cond.value, exact=op != "contains")
        return (df[~mask], None) if op == "not_equals" else (df[mask], None)
    return df, f"ignored filter {col} {op} {cond.value!r}"


_NAME_FILLER = _VENDOR_NOISE | {"and", "dba"}


def name_words(text: str) -> list[str]:
    """Lowercase words with legal suffixes and "and"/"&" dropped and plurals singularized, so "Blain & Associates"
    and "Blain and Associate, Inc." compare equal."""
    tokens = re.sub(r"[^a-z0-9]", " ", str(text).lower()).split()
    return [t[:-1] if len(t) > 3 and t.endswith("s") else t for t in tokens if t not in _NAME_FILLER]


def page_text(file_ids: list[str]) -> dict[str, str]:
    if not file_ids:
        return {}
    with get_engine().connect() as conn:
        rows = conn.execute(select(Page.file_id, Page.text).where(Page.file_id.in_(file_ids))).all()
    texts: dict[str, list[str]] = {}
    for r in rows:
        texts.setdefault(r.file_id, []).append(r.text or "")
    return {fid: "\n".join(parts) for fid, parts in texts.items()}


def _vendor_product_mask(df: pd.DataFrame, words: list[str], vendor_words: pd.Series) -> pd.Series:
    """Names like "Grey Wall Veoci" join a vendor and a product: the longest run of leading words that names exactly
    one vendor picks the vendor, and its documents must mention the remaining words."""
    none = pd.Series(False, index=df.index)
    for n in range(len(words) - 1, 0, -1):
        lead, rest = set(words[:n]), set(words[n:])
        mask = vendor_words.map(lambda v, lead=lead: lead <= v)
        if not mask.any():
            continue
        if vendor_groups(df.loc[mask, "vendor_name"]).nunique() != 1:
            return none
        texts = page_text(df.loc[mask, "file_id"].tolist())
        mentions = {fid for fid, text in texts.items() if rest <= set(name_words(text))}
        return mask & df["file_id"].isin(mentions)
    return none


def _name_mask(df: pd.DataFrame, name: str, fuzzy: bool = True) -> pd.Series:
    """Rows whose vendor has every word of `name`, or whose file name contains it (products like "Sympro" often
    appear only in the file name, not in the vendor's legal name). `fuzzy=False` keeps plain substring matching."""
    target = re.sub(r"[_\-.]+", " ", name.lower()).strip()
    if not target:
        return pd.Series(False, index=df.index)
    vendors = df["vendor_name"].fillna("").astype(str).str.lower()
    filenames = df["filename"].str.lower().str.replace(r"[_\-.]+", " ", regex=True)
    if len(target) <= 4:
        # Short names ("GTL", "CTI") must match a whole word, or "cti" would match "construction".
        word = rf"\b{re.escape(target)}\b"
        return vendors.str.contains(word, regex=True) | filenames.str.contains(word, regex=True)
    by_file = filenames.str.contains(target, regex=False)
    if not fuzzy:
        return vendors.str.contains(target, regex=False) | by_file
    words = name_words(target)
    if not words:
        return by_file
    vendor_words = df["vendor_name"].map(lambda v: set(name_words(v)) if isinstance(v, str) else set())
    mask = vendor_words.map(lambda v: set(words) <= v) | by_file
    if mask.any() or len(words) < 2:
        return mask
    return _vendor_product_mask(df, words, vendor_words)


def resolve_scope(df: pd.DataFrame, contract_numbers: list[str], vendor_names: list[str]) -> pd.DataFrame:
    """Named contract numbers win: a vendor only adds documents when none of the numbered ones are its own, so
    "contract 21163 with Appin" doesn't pull in Appin's other contracts."""
    if not contract_numbers and not vendor_names:
        return df
    number_mask = pd.Series(False, index=df.index)
    numbers = df["contract_number"].fillna("").astype(str).str.lower().str.replace("-", "")
    filenames = df["filename"].str.lower()
    for number in contract_numbers:
        digits = "".join(ch for ch in number if ch.isalnum()).lower()
        if not digits:
            continue
        number_mask |= numbers.str.contains(digits, regex=False)
        if len(digits) >= 5 and digits[:5].isdigit():
            number_mask |= df["family_id"].fillna("").astype(str).eq(digits[:5])
            number_mask |= filenames.str.contains(digits[:5], regex=False)
    mask = number_mask.copy()
    for vendor in vendor_names:
        vendor_mask = _name_mask(df, vendor)
        if not (vendor_mask & number_mask).any():
            mask |= vendor_mask
    return df[mask]


def names_contracts(plan: QueryPlan) -> bool:
    """True when the plan points at specific contracts (by number, vendor, or text), not the whole corpus."""
    named_fields = {"vendor_name", "contract_number", "family_id", "contract_description"}
    return bool(
        plan.scope_contract_numbers
        or plan.scope_vendor_names
        or any(c.field in named_fields and c.op in {"equals", "contains", "in_list"} for c in plan.filters)
    )


def _group_key(df: pd.DataFrame, group_by: str) -> pd.Series:
    if group_by == "expiration_year":
        return df["expiration_date"].map(lambda d: "Unknown" if pd.isna(d) else str(d.year))
    if group_by == "expiration_quarter":
        return df["expiration_date"].map(lambda d: "Unknown" if pd.isna(d) else f"{d.year}Q{d.quarter}")
    series = df["vendor_group"] if group_by == "vendor_name" and "vendor_group" in df else df[group_by]
    if group_by in BOOL_FIELDS:
        return series.map({True: "Yes", False: "No"}).fillna("Unknown")
    return series.fillna("Unknown").map(lambda v: ENUM_LABELS.get(v, v))


def _format_table(df: pd.DataFrame, extra: list[str]) -> pd.DataFrame:
    columns = list(dict.fromkeys([*DISPLAY_COLUMNS, *extra]))
    out = df[[c for c in columns if c in df.columns]].copy()
    for col in DATE_FIELDS & set(out.columns):
        out[col] = out[col].dt.date
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(lambda v: ENUM_LABELS.get(v, v) if isinstance(v, str) else v)
        if col in BOOL_FIELDS:
            out[col] = out[col].map({True: "Yes", False: "No"}).fillna("Unknown")
    return out.rename(columns=FIELD_LABELS)


def format_value(field_name: str, value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    kind = FIELD_BY_NAME[field_name].kind if field_name in FIELD_BY_NAME else "text"
    if kind == "bool":
        return "Yes" if value else "No"
    if kind == "amount":
        return f"${float(value):,.0f}"
    if kind == "days":
        return f"{int(value)} days"
    if kind == "date":
        return pd.Timestamp(value).date().isoformat()
    return ENUM_LABELS.get(str(value), str(value))


def _clip(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _describe(
    row: pd.Series,
    fields: list[str] | tuple[str, ...] = (),
    evidence: dict[tuple[str, str], tuple[str | None, int | None]] | None = None,
) -> str:
    vendor = row.get("vendor_name")
    name = vendor if isinstance(vendor, str) and vendor else "Unnamed vendor"
    bits = [ENUM_LABELS.get(str(row.get("document_type")), "Document")]
    if isinstance(row.get("contract_number"), str) and row.get("contract_number"):
        bits.append(f"contract {row['contract_number']}")
    expiration = row.get("expiration_date")
    if expiration is not None and not pd.isna(expiration):
        end = pd.Timestamp(expiration).date()
        if row.get("auto_renewal") in (True,) and end < date.today():
            bits.append(f"initial term ended {end.isoformat()}; auto-renews")
        else:
            bits.append(f"term ends {end.isoformat()}")
    lines = [f"- **{name}** · {', '.join(bits)} · `{row.get('filename')}`"]
    for f in fields:
        if f in {"vendor_name", "document_type"}:
            continue
        label = FIELD_LABELS.get(f, f)
        value = format_value(f, row.get(f))
        if value is None:
            lines.append(f"  - {label}: not stated in this document")
            continue
        quote, page = (evidence or {}).get((row["file_id"], f), (None, None))
        cite = f" (p.{page}: “{_clip(quote)}”)" if quote else ""
        lines.append(f"  - {label}: **{value}**{cite}")
    return "\n".join(lines)


def _distribution(df: pd.DataFrame, field_name: str) -> str | None:
    """One-line spread of a field's values across many documents (e.g. notice periods)."""
    spec = FIELD_BY_NAME.get(field_name)
    if spec is None or spec.kind == "text" or field_name not in df.columns:
        return None
    label = FIELD_LABELS.get(field_name, field_name)
    values = df[field_name]
    if spec.kind == "amount":
        values = pd.to_numeric(values, errors="coerce")
    stated = values.dropna()
    if stated.empty:
        return f"- **{label}:** not stated in any of these documents"
    if spec.kind in {"amount", "date"}:
        body = f"from {format_value(field_name, stated.min())} to {format_value(field_name, stated.max())}"
    else:
        counts = stated.map(lambda v: format_value(field_name, v)).value_counts()
        body = ", ".join(f"{v} in {n}" for v, n in counts.items())
    missing = len(values) - len(stated)
    return f"- **{label}:** {body}" + (f"; {missing} don't say" if missing else "")


def run_structured(plan: QueryPlan, df: pd.DataFrame | None = None) -> StructuredResult:
    df = load_contracts_frame() if df is None else df
    total = len(df)
    notes: list[str] = []
    scoped = resolve_scope(df, plan.scope_contract_numbers, plan.scope_vendor_names)
    if (plan.scope_contract_numbers or plan.scope_vendor_names) and scoped.empty:
        names = ", ".join([*plan.scope_contract_numbers, *plan.scope_vendor_names])
        notes.append(f"no extracted record matches {names}")
    scope_names = {n.strip().lower() for n in [*plan.scope_contract_numbers, *plan.scope_vendor_names]}
    filtered = scoped
    for cond in plan.filters:
        if (
            cond.field in {"vendor_name", "contract_number"}
            and (cond.value or "").strip().lower() in scope_names
        ):
            continue
        filtered, note = apply_filter(filtered, cond)
        if note:
            notes.append(note)

    filter_fields = [c.field for c in plan.filters if c.field in FIELD_BY_NAME]
    unknown = {
        f: int(scoped[f].isna().sum())
        for f in filter_fields
        if f in scoped.columns and scoped[f].isna().any()
    }
    for f, n in unknown.items():
        is_bool = f in BOOL_FIELDS or (f in FIELD_BY_NAME and FIELD_BY_NAME[f].kind == "bool")
        outcome = "rather than counted as 'no'" if is_bool else "of this result"
        notes.append(
            f"{n} of {len(scoped)} documents don't state {FIELD_LABELS.get(f, f).lower()}; they are left out {outcome}"
        )

    chart = None
    if plan.group_by and plan.metric != "list":
        key = _group_key(filtered, plan.group_by)
        label = FIELD_LABELS.get(plan.group_by, plan.group_by)
        if plan.metric == "count":
            grouped = filtered.groupby(key).size().rename("Documents")
        else:
            awards = dedupe_awards(filtered)
            agg = "sum" if plan.metric == "sum_award" else "mean"
            grouped = awards["award_amount_usd"].groupby(_group_key(awards, plan.group_by)).agg(agg)
            grouped = grouped.rename("Award amount (USD)")
            notes.append("award amounts are counted once per contract family and vendor")
        value_col = str(grouped.name)
        table = grouped.reset_index()
        table.columns = [label, value_col]
        by_time = plan.group_by in {"expiration_year", "expiration_quarter"}
        table = table.sort_values(label if by_time else value_col, ascending=by_time)
        if plan.limit:
            table = table.head(plan.limit)
        if plan.chart in {"bar", "timeline"} or (plan.chart == "none" and len(table) > 1):
            chart = px.bar(table, x=label, y=value_col, text_auto=True)
        elif plan.chart == "pie":
            chart = px.pie(table, names=label, values=value_col)
        summary = f"{len(filtered)} matching documents grouped by {label.lower()}."
        return StructuredResult(table, summary, len(filtered), filtered["file_id"].tolist(), chart, notes)

    if plan.metric == "count":
        summary = f"**{len(filtered)}** of {total} documents match."
    elif plan.metric in {"sum_award", "avg_award"}:
        awards = dedupe_awards(filtered)
        amounts = awards["award_amount_usd"]
        value = amounts.sum() if plan.metric == "sum_award" else (amounts.mean() if len(amounts) else 0.0)
        word = "Total" if plan.metric == "sum_award" else "Average"
        summary = (
            f"{word} stated award amount: **${value:,.0f}** across {len(amounts)} distinct "
            f"award{'' if len(amounts) == 1 else 's'} ({len(filtered)} matching "
            f"document{'' if len(filtered) == 1 else 's'}; awards restated in several documents are counted once)."
        )
        if 0 < len(awards) <= 8:
            award_evidence = load_evidence(awards["file_id"].tolist(), ["award_amount_usd"])
            summary += "\n\n" + "\n".join(
                _describe(row, ["award_amount_usd"], award_evidence) for _, row in awards.iterrows()
            )
    else:
        summary = f"**{len(filtered)}** of {total} documents match."

    ordered = filtered
    if plan.sort_by and plan.sort_by in filtered.columns:
        ordered = filtered.sort_values(plan.sort_by, ascending=not plan.sort_desc, na_position="last")
    elif "expiration_date" in filtered.columns:
        ordered = filtered.sort_values("expiration_date", na_position="last")
    limit = min(plan.limit or MAX_ROWS, MAX_ROWS)
    shown = ordered.head(limit)
    if len(ordered) > limit:
        notes.append(f"showing the first {limit} of {len(ordered)} matches")

    answer_fields = [f for f in dict.fromkeys(plan.answer_fields) if f in FIELD_BY_NAME]
    focus = list(dict.fromkeys([*answer_fields, *filter_fields]))
    evidence = load_evidence(shown["file_id"].tolist(), focus)
    if plan.metric == "list" and 0 < len(shown) <= 8:
        summary += "\n\n" + "\n".join(_describe(row, answer_fields, evidence) for _, row in shown.iterrows())
    elif answer_fields and len(filtered) > 8:
        spread = [line for f in answer_fields if (line := _distribution(filtered, f))]
        if spread:
            summary += "\n\n" + "\n".join(spread)

    table = _format_table(shown, [f for f in focus if f not in DISPLAY_COLUMNS])
    for f in focus:
        quotes = [evidence.get((fid, f), (None, None)) for fid in shown["file_id"]]
        if any(q for q, _ in quotes):
            table[f"{FIELD_LABELS.get(f, f)} evidence"] = [f"p.{p}: “{q}”" if q else "" for q, p in quotes]

    if plan.chart == "timeline" and shown["expiration_date"].notna().any():
        tl = shown.dropna(subset=["expiration_date"]).copy()
        tl["start"] = tl["effective_date"].fillna(tl["expiration_date"] - pd.Timedelta(days=30))
        tl["label"] = tl["vendor_name"].fillna(tl["filename"]).str.slice(0, 40)
        chart = px.timeline(tl, x_start="start", x_end="expiration_date", y="label", hover_name="filename")
        chart.update_yaxes(autorange="reversed", title=None)
        chart.add_vline(x=pd.Timestamp(date.today()), line_dash="dot")
    return StructuredResult(table, summary, len(filtered), filtered["file_id"].tolist(), chart, notes)
