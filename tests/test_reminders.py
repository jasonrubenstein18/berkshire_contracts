import json
from datetime import date

import httpx
import pandas as pd
import pytest

from berkshire_contracts import reminders
from berkshire_contracts.config import get_settings

TODAY = date(2026, 10, 13)


def _row(file_id, family_id, vendor, expires, **extra):
    row = {
        "file_id": file_id,
        "filename": f"{file_id}.pdf",
        "family_id": family_id,
        "vendor_name": vendor,
        "vendor_group": vendor,
        "contract_number": family_id,
        "document_type": "renewal_extension",
        "expiration_date": pd.Timestamp(expires) if expires else pd.NaT,
        "auto_renewal": None,
        "renewal_notice_days": None,
        "needs_review": False,
    }
    row.update(extra)
    return row


def _frame(*rows):
    return pd.DataFrame(list(rows))


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    for name in ("RESEND_API_KEY", "REMINDER_FROM", "REMINDER_TO"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("REMINDER_DAYS", "14")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_family_uses_its_latest_expiration():
    df = _frame(
        _row("a1", "F1", "Acme", "2026-10-20", document_type="agreement"),
        _row("a2", "F1", "Acme", "2027-10-20"),
        _row("b1", "F2", "Bolt", "2025-10-20", document_type="agreement"),
        _row("b2", "F2", "Bolt", "2026-10-21", renewal_notice_days=30, auto_renewal=True),
    )
    due = reminders.expiring(14, TODAY, df=df, evidence={("b2", "expiration_date"): ("quote", 2)})
    assert due["vendor"].tolist() == ["Bolt"]
    first = due.iloc[0]
    assert first["expires"] == date(2026, 10, 21)
    assert first["days_left"] == 8
    assert first["act_by"] == date(2026, 9, 21)
    assert bool(first["auto_renewal"])
    assert first["filename"] == "b2.pdf"
    assert first["page"] == 2


def test_window_boundaries_and_files_without_family():
    df = _frame(
        _row("d0", "F0", "Today Co", "2026-10-13"),
        _row("d14", "F14", "Edge Co", "2026-10-27"),
        _row("d15", "F15", "Late Co", "2026-10-28"),
        _row("past", "FP", "Past Co", "2026-10-12"),
        _row("solo", None, "Solo Co", "2026-10-15"),
        _row("nodate", "FN", "No Date Co", None),
    )
    due = reminders.expiring(14, TODAY, df=df, evidence={})
    assert due["vendor"].tolist() == ["Today Co", "Solo Co", "Edge Co"]
    assert due["days_left"].tolist() == [0, 2, 14]


def test_separate_vendors_in_one_family_are_separate_contracts():
    df = _frame(
        _row("c1", "F1", "Ciorba Group", "2026-10-18"),
        _row("c2", "F1", "Clark Dietz", "2026-10-18"),
    )
    assert len(reminders.expiring(14, TODAY, df=df, evidence={})) == 2


def test_tie_prefers_the_file_that_states_renewal_terms():
    df = _frame(
        _row("z_rate", "F1", "Acme", "2026-10-18", document_type="price_adjustment"),
        _row("a_renewal", "F1", "Acme", "2026-10-18", auto_renewal=False),
    )
    due = reminders.expiring(14, TODAY, df=df, evidence={})
    assert due.iloc[0]["filename"] == "a_renewal.pdf"


def test_render_escapes_html_and_flags_review():
    df = _frame(_row("x1", "F1", "<script>alert(1)</script> & Co", "2026-10-20", needs_review=True))
    due = reminders.expiring(14, TODAY, df=df, evidence={("x1", "expiration_date"): ("q", 3)})
    subject, body, text = reminders.render(due, TODAY, 14)
    assert subject == "1 contract expires by Oct 27, 2026"
    assert "<script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; Co" in body
    assert "verify date on p.3" in body
    assert "Verify date on p.3." in text
    assert "https://berkshire-contracts.fly.dev" in body


def test_render_with_nothing_due():
    subject, body, text = reminders.render(pd.DataFrame(columns=reminders.COLUMNS), TODAY, 14)
    assert subject == "No contracts expire in the next 14 days"
    assert "Nothing expires" in body
    assert "Nothing expires" in text


def test_send_without_key_does_not_call_resend():
    def fail(request):
        raise AssertionError("Resend must not be called")

    client = httpx.Client(transport=httpx.MockTransport(fail))
    assert reminders.send("s", "<p>h</p>", "t", TODAY, client=client) == {
        "sent": False,
        "reason": "RESEND_API_KEY not set",
    }


def test_send_requires_sender_and_recipients(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    get_settings.cache_clear()
    result = reminders.send("s", "<p>h</p>", "t", TODAY)
    assert result == {"sent": False, "reason": "REMINDER_FROM and REMINDER_TO must be set"}


def test_send_posts_payload_with_daily_idempotency_key(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("REMINDER_FROM", "Contracts <reminders@example.com>")
    monkeypatch.setenv("REMINDER_TO", "a@example.com, b@example.com")
    get_settings.cache_clear()
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "email_123"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = reminders.send("Subject", "<p>h</p>", "t", TODAY, client=client)
    assert result == {"sent": True, "id": "email_123"}
    assert seen["url"] == reminders.RESEND_URL
    assert seen["headers"]["authorization"] == "Bearer re_test"
    assert seen["headers"]["idempotency-key"] == "contracts-reminder-2026-10-13"
    assert seen["body"] == {
        "from": "Contracts <reminders@example.com>",
        "to": ["a@example.com", "b@example.com"],
        "subject": "Subject",
        "html": "<p>h</p>",
        "text": "t",
    }


def test_send_reports_resend_errors(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("REMINDER_FROM", "reminders@example.com")
    monkeypatch.setenv("REMINDER_TO", "a@example.com")
    get_settings.cache_clear()
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(403, json={"message": "domain not verified"}))
    )
    result = reminders.send("s", "h", "t", TODAY, client=client)
    assert result["sent"] is False
    assert result["error"] == "Resend returned 403"
    assert result["message"] == "domain not verified"


def test_run_returns_counts_only(monkeypatch):
    df = _frame(_row("a1", "F1", "Acme", "2026-10-20"))
    monkeypatch.setattr(reminders, "load_contracts_frame", lambda: df)
    monkeypatch.setattr(reminders, "load_evidence", lambda ids, fields: {})
    result = reminders.run(14, TODAY)
    assert result == {
        "date": "2026-10-13",
        "days": 14,
        "contracts": 1,
        "sent": False,
        "reason": "RESEND_API_KEY not set",
    }
