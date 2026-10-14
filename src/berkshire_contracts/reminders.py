"""Daily expiry email: one message listing every contract that expires within the next N days.

A contract is a family and vendor (a family can hold separate awards, e.g. 22128-1 and 22128-2), and its
expiration is the latest one across its files, so an agreement a renewal has replaced does not alert.
Without RESEND_API_KEY the email is built but not sent.
"""

from __future__ import annotations

import html
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from berkshire_contracts.chat.filters import load_contracts_frame, load_evidence
from berkshire_contracts.config import get_settings
from berkshire_contracts.extraction.fields import ENUM_LABELS

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
COLUMNS = [
    "expires",
    "days_left",
    "vendor",
    "contract_number",
    "document_type",
    "auto_renewal",
    "act_by",
    "filename",
    "page",
    "needs_review",
]


def local_today() -> date:
    return datetime.now(ZoneInfo(get_settings().reminder_timezone)).date()


def _text(value: object) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NaT:
        return None
    text = str(value).strip()
    return text or None


def expiring(
    days: int | None = None,
    today: date | None = None,
    df: pd.DataFrame | None = None,
    evidence: dict[tuple[str, str], tuple[str | None, int | None]] | None = None,
) -> pd.DataFrame:
    """Contracts whose latest expiration falls between today and today + days, inclusive, soonest first."""
    days = get_settings().reminder_days if days is None else days
    today = today or local_today()
    df = load_contracts_frame() if df is None else df
    dated = df.dropna(subset=["expiration_date"]).copy()
    if dated.empty:
        return pd.DataFrame(columns=COLUMNS)
    dated["expiration_date"] = pd.to_datetime(dated["expiration_date"])
    vendor = dated["vendor_group"] if "vendor_group" in dated else dated["vendor_name"]
    dated["contract_key"] = dated["family_id"].fillna(dated["file_id"]) + "|" + vendor.fillna("")
    # On a tie for the latest date, prefer the file that states renewal terms (a renewal letter over a rate change).
    terms = [c for c in ("auto_renewal", "renewal_notice_days") if c in dated]
    dated["has_terms"] = dated[terms].notna().any(axis=1) if terms else False
    latest = dated.sort_values(["expiration_date", "has_terms", "file_id"]).drop_duplicates(
        "contract_key", keep="last"
    )
    start = pd.Timestamp(today)
    due = latest[
        (latest["expiration_date"] >= start) & (latest["expiration_date"] <= start + pd.Timedelta(days=days))
    ].sort_values(["expiration_date", "vendor_name"])
    if due.empty:
        return pd.DataFrame(columns=COLUMNS)
    if evidence is None:
        evidence = load_evidence(due["file_id"].tolist(), ["expiration_date"])

    rows = []
    for _, r in due.iterrows():
        expires = r["expiration_date"].date()
        notice = pd.to_numeric(r.get("renewal_notice_days"), errors="coerce")
        auto = r.get("auto_renewal")
        rows.append(
            {
                "expires": expires,
                "days_left": (expires - today).days,
                "vendor": _text(r.get("vendor_name")),
                "contract_number": _text(r.get("contract_number")),
                "document_type": ENUM_LABELS.get(str(r.get("document_type")), _text(r.get("document_type"))),
                "auto_renewal": None if auto is None or pd.isna(auto) else bool(auto),
                "act_by": expires - timedelta(days=int(notice)) if pd.notna(notice) else None,
                "filename": r["filename"],
                "page": evidence.get((r["file_id"], "expiration_date"), (None, None))[1],
                "needs_review": bool(r.get("needs_review")),
            }
        )
    return pd.DataFrame(rows, columns=COLUMNS)


def _day(value: date) -> str:
    return f"{value:%b} {value.day}, {value.year}"


def _yes_no(value: bool | None) -> str:
    return "Unknown" if value is None else ("Yes" if value else "No")


def _source(row: pd.Series) -> str:
    return f"{row['filename']}, p.{row['page']}" if pd.notna(row["page"]) else str(row["filename"])


def _verify(row: pd.Series) -> str:
    if not row["needs_review"]:
        return ""
    return f"verify date on p.{row['page']}" if pd.notna(row["page"]) else "verify date in the file"


def render(rows: pd.DataFrame, today: date, days: int | None = None) -> tuple[str, str, str]:
    """Subject, HTML body and plain-text body. Every document-derived value is HTML-escaped."""
    settings = get_settings()
    days = settings.reminder_days if days is None else days
    end = today + timedelta(days=days)
    n = len(rows)
    if n == 0:
        subject = f"No contracts expire in the next {days} days"
    else:
        subject = f"{n} contract{'s' if n != 1 else ''} expire{'s' if n == 1 else ''} by {_day(end)}"
    intro = f"Contracts expiring between {_day(today)} and {_day(end)}."
    footer = f"Check each date against the cited page before acting. Open the app: {settings.app_url}"

    lines = [intro, ""]
    if n == 0:
        lines.append(f"Nothing expires in the next {days} days.")
    for _, r in rows.iterrows():
        act_by = _day(r["act_by"]) if pd.notna(r["act_by"]) else "not stated"
        parts = [
            f"- {_day(r['expires'])} ({r['days_left']} days): {r['vendor'] or 'Unknown vendor'},",
            f"contract {r['contract_number'] or 'not stated'}, {r['document_type'] or 'unknown type'}.",
            f"Auto-renews: {_yes_no(r['auto_renewal'])}. Act by: {act_by}. Source: {_source(r)}.",
        ]
        if r["needs_review"]:
            parts.append(_verify(r).capitalize() + ".")
        lines.append(" ".join(parts))
    lines += ["", footer]
    text = "\n".join(lines)

    e = html.escape
    headers = [
        "Expires",
        "Days left",
        "Vendor",
        "Contract #",
        "Type",
        "Auto-renews",
        "Act by",
        "Source",
        "Check",
    ]
    cell = 'style="border:1px solid #d0d7de;padding:6px 8px;text-align:left;vertical-align:top"'
    head = "".join(f"<th {cell}>{e(h)}</th>" for h in headers)
    body = []
    for _, r in rows.iterrows():
        values = [
            _day(r["expires"]),
            str(r["days_left"]),
            r["vendor"] or "Unknown",
            r["contract_number"] or "—",
            r["document_type"] or "—",
            _yes_no(r["auto_renewal"]),
            _day(r["act_by"]) if pd.notna(r["act_by"]) else "—",
            _source(r),
            _verify(r),
        ]
        body.append("<tr>" + "".join(f"<td {cell}>{e(v)}</td>" for v in values) + "</tr>")
    table = (
        f'<table style="border-collapse:collapse;font-size:14px"><tr>{head}</tr>{"".join(body)}</table>'
        if body
        else f"<p>Nothing expires in the next {days} days.</p>"
    )
    url = e(settings.app_url)
    html_body = (
        '<div style="font-family:Arial,sans-serif;color:#1f2328">'
        f"<p>{e(intro)}</p>{table}"
        f'<p>Check each date against the cited page before acting. <a href="{url}">Open the app</a>.</p>'
        "</div>"
    )
    return subject, html_body, text


def send(subject: str, html_body: str, text: str, today: date, client: httpx.Client | None = None) -> dict:
    """Send through Resend. One idempotency key per day, so a rerun the same day does not email twice."""
    settings = get_settings()
    key = settings.resend_api_key.get_secret_value().strip() if settings.resend_api_key else ""
    if not key:
        return {"sent": False, "reason": "RESEND_API_KEY not set"}
    recipients = settings.reminder_recipients
    if not settings.reminder_from or not recipients:
        return {"sent": False, "reason": "REMINDER_FROM and REMINDER_TO must be set"}
    payload = {
        "from": settings.reminder_from,
        "to": recipients,
        "subject": subject,
        "html": html_body,
        "text": text,
    }
    headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": f"contracts-reminder-{today.isoformat()}"}
    http = client or httpx.Client(timeout=20)
    try:
        response = http.post(RESEND_URL, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("Resend request failed: %s", type(exc).__name__)
        return {"sent": False, "error": "Resend request failed"}
    finally:
        if client is None:
            http.close()
    if response.status_code >= 300:
        try:
            message = str(response.json().get("message", ""))[:200]
        except ValueError:
            message = ""
        log.warning("Resend returned %s: %s", response.status_code, message)
        return {"sent": False, "error": f"Resend returned {response.status_code}", "message": message}
    return {"sent": True, "id": response.json().get("id")}


def run(days: int | None = None, today: date | None = None, dry_run: bool = False) -> dict:
    """Build today's email and send it unless dry_run. Returns a count and the send result, no contract details."""
    days = get_settings().reminder_days if days is None else days
    today = today or local_today()
    rows = expiring(days, today)
    subject, html_body, text = render(rows, today, days)
    result: dict = {"date": today.isoformat(), "days": days, "contracts": len(rows)}
    if dry_run:
        result.update(sent=False, reason="dry run")
    else:
        result.update(send(subject, html_body, text, today))
    return result
