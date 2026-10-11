"""Data access and figures for the Gradio app."""

from __future__ import annotations

import json
import re
import textwrap
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import pymupdf
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import func, select

from berkshire_contracts.chat.filters import FIELD_LABELS, dedupe_awards, load_contracts_frame
from berkshire_contracts.config import get_settings
from berkshire_contracts.db import Contract, FieldEvidence, LlmCall, Page, get_engine, session_scope
from berkshire_contracts.extraction.fields import ENUM_LABELS, FIELD_SPECS
from berkshire_contracts.schemas import EXTRACTION_FIELDS

PLOT_TEMPLATE = "plotly_white"
ACCENT = "#1f3a5f"
PALETTE = ["#1f3a5f", "#3d6a99", "#6f9bc8", "#a7c4e2", "#c9a227", "#8c8c8c", "#5a8f6b", "#b5543c"]
px.defaults.template = PLOT_TEMPLATE
px.defaults.color_discrete_sequence = PALETTE


def _style(fig: go.Figure, height: int = 320) -> go.Figure:
    fig.update_layout(
        template=PLOT_TEMPLATE,
        height=height,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        colorway=PALETTE,
        font={"size": 12},
        title_font={"size": 14},
        legend_title_text="",
    )
    return fig


def label(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "Unknown"
    return ENUM_LABELS.get(str(value), str(value))


def money(value: float | None) -> str:
    if value is None or pd.isna(value):
        return "—"
    if value >= 1e6:
        return f"${value / 1e6:,.1f}M"
    if value >= 1e3:
        return f"${value / 1e3:,.0f}K"
    return f"${value:,.0f}"


def kpis(df: pd.DataFrame | None = None) -> dict[str, str]:
    df = load_contracts_frame() if df is None else df
    today = pd.Timestamp(date.today())
    horizon = today + pd.Timedelta(days=365)
    with session_scope() as s:
        files = s.query(func.count(Contract.file_id)).scalar() or 0
        found = s.query(func.count(FieldEvidence.id)).filter(FieldEvidence.status == "found").scalar() or 0
        verified = (
            s.query(func.count(FieldEvidence.id))
            .filter(FieldEvidence.status == "found", FieldEvidence.verified.is_(True))
            .scalar()
            or 0
        )
    expiring = df[(df["expiration_date"] >= today) & (df["expiration_date"] <= horizon)]
    return {
        "Files in sample": f"{files}",
        "Unique documents": f"{len(df)}",
        "Contract families": f"{df['family_id'].nunique()}",
        "Stated award value": money(dedupe_awards(df)["award_amount_usd"].sum()),
        "Auto-renewing": f"{int((df['auto_renewal'] == True).sum())}",  # noqa: E712
        "Expiring in 12 months": f"{len(expiring)}",
        "Flagged for review": f"{int(df['needs_review'].fillna(False).astype(bool).sum())}",
        "Evidence verified": f"{verified / found:.0%}" if found else "—",
    }


def kpi_html(values: dict[str, str]) -> str:
    cards = "".join(
        f'<div class="kpi"><div class="kpi-value">{v}</div><div class="kpi-label">{k}</div></div>'
        for k, v in values.items()
    )
    return f'<div class="kpi-grid">{cards}</div>'


def overview_figures(df: pd.DataFrame | None = None) -> dict[str, go.Figure]:
    df = load_contracts_frame() if df is None else df
    figs: dict[str, go.Figure] = {}

    types = (
        df["document_type"]
        .map(label)
        .value_counts()
        .rename_axis("Document type")
        .reset_index(name="Documents")
    )
    figs["types"] = _style(
        px.bar(types, x="Documents", y="Document type", orientation="h", title="Documents by type")
    )
    figs["types"].update_yaxes(categoryorder="total ascending", title=None)

    stated = df["renewal_type"].dropna()
    renewal = stated.map(label).value_counts().rename_axis("Renewal").reset_index(name="Documents")
    figs["renewal"] = _style(
        px.bar(
            renewal.sort_values("Documents"),
            x="Documents",
            y="Renewal",
            orientation="h",
            text="Documents",
            title=f"Who controls renewal ({len(stated)} documents state it; {len(df) - len(stated)} don't)",
        )
    )

    exp = df.dropna(subset=["expiration_date"]).copy()
    exp = exp[
        (exp["expiration_date"] >= pd.Timestamp("2018-01-01"))
        & (exp["expiration_date"] <= pd.Timestamp("2032-12-31"))
    ]
    exp["Year"] = exp["expiration_date"].dt.year.astype(str)
    exp["Status"] = (exp["expiration_date"] < pd.Timestamp(date.today())).map(
        {True: "Expired", False: "Active"}
    )
    by_year = exp.groupby(["Year", "Status"]).size().reset_index(name="Documents")
    figs["expirations"] = _style(
        px.bar(
            by_year,
            x="Year",
            y="Documents",
            color="Status",
            title="Term end dates by year",
            color_discrete_map={"Expired": "#a7c4e2", "Active": ACCENT},
        )
    )

    vendors = (
        dedupe_awards(df)
        .dropna(subset=["vendor_group"])
        .groupby("vendor_group")["award_amount_usd"]
        .sum()
        .sort_values(ascending=False)
        .head(10)
        .rename_axis("Vendor")
        .reset_index(name="Stated award (USD)")
    )
    vendors["Vendor"] = vendors["Vendor"].str.slice(0, 38)
    figs["vendors"] = _style(
        px.bar(
            vendors,
            x="Stated award (USD)",
            y="Vendor",
            orientation="h",
            title="Top vendors by stated award value",
        )
    )
    figs["vendors"].update_yaxes(categoryorder="total ascending", title=None)

    coverage = pd.DataFrame(
        {
            "Field": [FIELD_LABELS[f] for f in EXTRACTION_FIELDS],
            "Coverage": [df[f].notna().mean() for f in EXTRACTION_FIELDS],
        }
    )
    figs["coverage"] = _style(
        px.bar(
            coverage,
            x="Coverage",
            y="Field",
            orientation="h",
            title="Share of documents with each field",
            range_x=[0, 1],
        ),
        height=520,
    )
    figs["coverage"].update_xaxes(tickformat=".0%")
    figs["coverage"].update_yaxes(categoryorder="total ascending", title=None)

    risk = pd.DataFrame(
        {
            "Clause": [
                "Termination for convenience",
                "Liability cap",
                "Vendor indemnifies County",
                "Assignment needs consent",
            ],
            "Documents with clause": [
                int((df["termination_for_convenience"] == True).sum()),  # noqa: E712
                int(df["liability_cap"].notna().sum()),
                int((df["indemnification"] == "vendor_indemnifies_county").sum()),
                int((df["assignment_change_of_control"] == "consent_required").sum()),
            ],
        }
    )
    figs["risk"] = _style(
        px.bar(risk, x="Documents with clause", y="Clause", orientation="h", title="Risk-clause coverage")
    )
    figs["risk"].update_yaxes(title=None)
    return figs


def upcoming_renewals(df: pd.DataFrame | None = None, days: int = 540) -> pd.DataFrame:
    df = load_contracts_frame() if df is None else df
    today = pd.Timestamp(date.today())
    soon = df[
        (df["expiration_date"] >= today) & (df["expiration_date"] <= today + pd.Timedelta(days=days))
    ].copy()
    soon = soon.sort_values("expiration_date")
    notice = pd.to_numeric(soon["renewal_notice_days"], errors="coerce")
    soon["Act by"] = [
        (exp - timedelta(days=int(n))).date() if pd.notna(n) else None
        for exp, n in zip(soon["expiration_date"], notice, strict=True)
    ]
    out = pd.DataFrame(
        {
            "Expires": soon["expiration_date"].dt.date,
            "Act by": soon["Act by"],
            "Vendor": soon["vendor_name"],
            "Contract #": soon["contract_number"],
            "Document": soon["document_type"].map(label),
            "Renewal": soon["renewal_type"].map(label),
            "Auto-renews": soon["auto_renewal"].map({True: "Yes", False: "No"}).fillna("Unknown"),
            "File": soon["filename"],
        }
    )
    return out.reset_index(drop=True)


EXPLORER_COLUMNS = {
    "filename": "File",
    "contract_number": "Contract #",
    "document_type": "Type",
    "vendor_name": "Vendor",
    "effective_date": "Effective",
    "expiration_date": "Expires",
    "award_amount_usd": "Award (USD)",
    "renewal_type": "Renewal",
    "auto_renewal": "Auto-renews",
    "termination_for_convenience": "Term. for convenience",
    "fields_found": "Fields found",
    "needs_review": "Needs review",
    "text_source": "Text",
    "file_id": "File ID",
}


def explorer_table(
    doc_types: list[str] | None = None,
    vendor_query: str = "",
    auto_renewal: str = "Any",
    review_only: bool = False,
    text_query: str = "",
) -> pd.DataFrame:
    df = load_contracts_frame(include_duplicates=True)
    if doc_types:
        wanted = {k for k, v in ENUM_LABELS.items() if v in doc_types} | set(doc_types)
        df = df[df["document_type"].isin(wanted)]
    if vendor_query.strip():
        df = df[df["vendor_name"].fillna("").str.contains(vendor_query.strip(), case=False, regex=False)]
    if auto_renewal in {"Yes", "No"}:
        df = df[df["auto_renewal"] == (auto_renewal == "Yes")]
    if review_only:
        df = df[df["needs_review"].fillna(False).astype(bool)]
    if text_query.strip():
        q = text_query.strip()
        hay = (
            df[["filename", "contract_number", "contract_description", "vendor_name"]]
            .fillna("")
            .agg(" ".join, axis=1)
        )
        df = df[hay.str.contains(q, case=False, regex=False)]
    out = df[list(EXPLORER_COLUMNS)].copy()
    for col in ("effective_date", "expiration_date"):
        out[col] = out[col].dt.date
    for col in ("document_type", "renewal_type"):
        out[col] = out[col].map(label)
    for col in ("auto_renewal", "termination_for_convenience", "needs_review"):
        out[col] = out[col].map({True: "Yes", False: "No"}).fillna("Unknown")
    out["award_amount_usd"] = pd.to_numeric(out["award_amount_usd"], errors="coerce").round(0)
    return out.rename(columns=EXPLORER_COLUMNS).sort_values("File").reset_index(drop=True)


def contract_detail(file_id: str) -> tuple[str, pd.DataFrame, list[tuple[str, int]]]:
    with session_scope() as s:
        c = s.get(Contract, file_id)
        if c is None:
            return "Contract not found.", pd.DataFrame(), []
        evidence = {e.field_name: e for e in s.query(FieldEvidence).filter(FieldEvidence.file_id == file_id)}
        review = c.review_reasons or "none"
        header = (
            f"### {c.filename}\n"
            f"**Type:** {label(c.document_type)} · **Vendor:** {c.vendor_name or '—'} · "
            f"**Contract #:** {c.contract_number or '—'} · **Family:** {c.family_id or '—'}\n\n"
            f"**Pages:** {c.page_count} ({c.text_source}, {c.ocr_pages or 0} OCR) · "
            f"**Parse:** {c.parse_status} · **Extraction:** {c.extraction_status} ({c.extraction_model or '—'})\n\n"
            f"**Review flags:** {review}"
        )
        if c.duplicate_of:
            header += f"\n\n_Exact duplicate of `{c.duplicate_of}`; values are copied from that file._"
        rows, pages = [], []
        for spec in FIELD_SPECS:
            ev = evidence.get(spec.name)
            value = getattr(c, spec.name)
            if isinstance(value, bool):
                value = "Yes" if value else "No"
            elif isinstance(value, float) and spec.kind == "amount":
                value = f"${value:,.2f}"
            elif value is not None:
                value = label(value)
            rows.append(
                {
                    "Field": spec.label,
                    "Value": value if value is not None else "—",
                    "Page": ev.page if ev and ev.status == "found" else None,
                    "Confidence": ev.confidence if ev and ev.status == "found" else "",
                    "Quote verified": ("Yes" if ev.verified else "No") if ev and ev.status == "found" else "",
                    "Evidence": ev.quote if ev and ev.status == "found" else "",
                }
            )
            if ev and ev.status == "found" and ev.page:
                pages.append((f"{spec.label} (p.{ev.page})", ev.page))
        return header, pd.DataFrame(rows), pages


def evidence_quote(file_id: str, page: int) -> str | None:
    with session_scope() as s:
        ev = (
            s.query(FieldEvidence)
            .filter(
                FieldEvidence.file_id == file_id, FieldEvidence.page == page, FieldEvidence.status == "found"
            )
            .first()
        )
        return ev.quote if ev else None


def _highlight_rects(page: pymupdf.Page, text: str | None) -> list[pymupdf.Rect]:
    if not text:
        return []
    words = re.sub(r"\s+", " ", text).strip().split(" ")
    rects: list[pymupdf.Rect] = []
    for size in (12, 8, 5):
        for start in range(0, len(words), size):
            piece = words[start : start + size]
            fragment = " ".join(piece)
            if len(piece) < 3 or len(fragment) < 12:
                continue
            rects.extend(page.search_for(fragment, quads=False))
        if rects:
            break
    return rects


@lru_cache(maxsize=64)
def render_page(
    filename: str, page_no: int, highlight: str | None = None, dpi: int = 110
) -> Image.Image | None:
    path = get_settings().sample_pdf_dir / filename
    if not path.exists():
        return None
    with pymupdf.open(path) as doc:
        if not 1 <= page_no <= doc.page_count:
            return None
        page = doc[page_no - 1]
        rects = _highlight_rects(page, highlight)
        for rect in rects:
            annot = page.add_highlight_annot(rect)
            annot.set_colors(stroke=(1.0, 0.85, 0.2))
            annot.update()
        pix = page.get_pixmap(dpi=dpi)
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if highlight and not rects:
        image = _with_quote_banner(image, highlight)
    return image


def _with_quote_banner(image: Image.Image, quote: str) -> Image.Image:
    """Scanned pages have no text layer to search, so show the OCR'd quote above the page instead."""
    font = ImageFont.load_default(size=17)
    lines = textwrap.wrap(" ".join(quote.split()), width=88)[:4]
    header = "Scanned page: quote read by OCR (not located on the image)"
    height = 22 * (len(lines) + 1) + 20
    banner = Image.new("RGB", (image.width, image.height + height), "white")
    draw = ImageDraw.Draw(banner)
    draw.rectangle((0, 0, image.width, height - 6), fill=(255, 236, 160))
    draw.text((14, 8), header, fill=(90, 70, 0), font=font)
    for i, line in enumerate(lines, start=1):
        draw.text((14, 8 + 22 * i), f"“{line}”" if len(lines) == 1 else line, fill=(30, 30, 30), font=font)
    banner.paste(image, (0, height))
    return banner


def pdf_path(filename: str) -> str | None:
    path = get_settings().sample_pdf_dir / filename
    return str(path) if path.exists() else None


def quality_tables() -> dict[str, pd.DataFrame]:
    with get_engine().connect() as conn:
        contracts = pd.read_sql(select(Contract.__table__), conn)
        pages = pd.read_sql(select(Page.method, Page.ocr_confidence, Page.rotation), conn)
        evidence = pd.read_sql(select(FieldEvidence.__table__), conn)
        calls = pd.read_sql(select(LlmCall.__table__), conn)
    unique = contracts[contracts["duplicate_of"].isna()]
    parse = pd.DataFrame(
        [
            {"Metric": "Files in sample", "Value": len(contracts)},
            {"Metric": "Unique documents (exact duplicates indexed once)", "Value": len(unique)},
            {"Metric": "Pages processed", "Value": len(pages)},
            {"Metric": "Pages read with OCR", "Value": int((pages["method"] == "ocr").sum())},
            {
                "Metric": "Scanned pages auto-rotated before OCR",
                "Value": int((pages["rotation"].fillna(0) != 0).sum()),
            },
            {
                "Metric": "Mean OCR confidence",
                "Value": round(float(pages["ocr_confidence"].dropna().mean()), 1),
            },
            {"Metric": "Pages with no recoverable text", "Value": int((pages["method"] == "empty").sum())},
            {"Metric": "Documents parsed fully", "Value": int((unique["parse_status"] == "ok").sum())},
            {
                "Metric": "Documents parsed partially",
                "Value": int((unique["parse_status"] == "partial").sum()),
            },
            {
                "Metric": "Documents that failed to open",
                "Value": int((unique["parse_status"] == "failed").sum()),
            },
        ]
    )
    status = (
        contracts["extraction_status"]
        .value_counts()
        .rename_axis("Extraction status")
        .reset_index(name="Files")
    )
    found = evidence[(evidence["status"] == "found") & evidence["file_id"].isin(unique["file_id"])]
    per_field = (
        found.groupby("field_name")
        .agg(
            found=("id", "count"),
            verified=("verified", "mean"),
            high_conf=("confidence", lambda s: (s == "high").mean()),
        )
        .reindex(list(EXTRACTION_FIELDS))
        .reset_index()
    )
    per_field["Field"] = per_field["field_name"].map(FIELD_LABELS)
    per_field["Documents with value"] = per_field["found"].fillna(0).astype(int)
    per_field["Quote verified"] = (per_field["verified"].fillna(0) * 100).round(0).astype(int).astype(
        str
    ) + "%"
    per_field["High confidence"] = (per_field["high_conf"].fillna(0) * 100).round(0).astype(int).astype(
        str
    ) + "%"
    per_field = per_field[["Field", "Documents with value", "Quote verified", "High confidence"]]
    reasons = (
        unique["review_reasons"]
        .dropna()
        .str.split("; ")
        .explode()
        .str.replace(r":.*", "", regex=True)
        .value_counts()
        .rename_axis("Review reason")
        .reset_index(name="Documents")
    )
    real = calls[(calls["cache_hit"] == False) & calls["error"].isna()]  # noqa: E712
    cost = (
        real.groupby(["purpose", "model"])
        .agg(
            calls=("id", "count"),
            input_tokens=("input_tokens", "sum"),
            output_tokens=("output_tokens", "sum"),
            cost_usd=("cost_usd", "sum"),
            median_latency_s=("latency_ms", lambda s: round(s.median() / 1000, 1)),
        )
        .reset_index()
    )
    cost["cost_usd"] = cost["cost_usd"].round(3)
    cost = cost.rename(
        columns={
            "purpose": "Purpose",
            "model": "Model",
            "calls": "Calls",
            "input_tokens": "Input tokens",
            "output_tokens": "Output tokens",
            "cost_usd": "Cost (USD)",
            "median_latency_s": "Median latency (s)",
        }
    )
    return {"parse": parse, "status": status, "fields": per_field, "reasons": reasons, "cost": cost}


def load_eval_summary() -> dict | None:
    path = get_settings().eval_results_dir / "summary.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def eval_markdown_path() -> Path:
    return get_settings().eval_results_dir / "report.md"
