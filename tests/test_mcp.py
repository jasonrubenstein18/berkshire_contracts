import asyncio

import pandas as pd
import pytest
from starlette.testclient import TestClient

from berkshire_contracts import mcp_server, reminders
from berkshire_contracts.chat.service import AssistantResponse, Source
from berkshire_contracts.config import get_settings
from berkshire_contracts.mcp_server import current_client, format_response, require_bearer


def _source(sid: str) -> Source:
    return Source(
        sid=sid,
        file_id="f",
        filename="16249_Motorola_Lease__Waukegan.pdf",
        page=1,
        section="Term",
        score=0.9,
        text="five years",
    )


def test_format_response_includes_table_and_cited_page_only():
    response = AssistantResponse(
        answer="The lease renews with notice.",
        route="clause_search",
        table=pd.DataFrame([{"vendor": "Motorola", "amount": 10}]),
        sources=[_source("S1"), _source("S2")],
        cited=["S1"],
        notes=["searched contract text"],
    )
    text = format_response(response)
    assert "The lease renews with notice." in text
    assert "Motorola" in text
    assert "[S1] 16249_Motorola_Lease__Waukegan.pdf, p.1, Term" in text
    assert "[S2]" not in text
    assert "searched contract text" in text


def test_format_response_lists_passages_when_nothing_was_cited():
    response = AssistantResponse(answer="Passages only.", route="clause_search", sources=[_source("S1")])
    assert "[S1]" in format_response(response)


def _guarded_call(guarded, headers: list[tuple[bytes, bytes]]) -> int:
    status = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    asyncio.run(guarded({"type": "http", "headers": headers}, receive, send))
    return status["code"]


def test_bearer_rejects_missing_and_wrong_tokens_and_names_the_client():
    clients = []

    async def app(scope, receive, send):
        clients.append(current_client())
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guarded = require_bearer(app, {"slack": "slack-token", "copilot": "copilot-token"})

    assert _guarded_call(guarded, []) == 401
    assert _guarded_call(guarded, [(b"authorization", b"Bearer wrong")]) == 401
    assert _guarded_call(guarded, [(b"x-api-key", b"wrong")]) == 401
    assert _guarded_call(guarded, [(b"authorization", b"slack-token")]) == 401
    assert _guarded_call(guarded, [(b"authorization", b"Bearer slack-token")]) == 200
    assert _guarded_call(guarded, [(b"x-api-key", b"copilot-token")]) == 200
    assert _guarded_call(guarded, [(b"authorization", b"Bearer copilot-token")]) == 200
    assert clients == ["slack", "copilot", "copilot"]
    assert current_client() == "unknown"


def test_bearer_rejects_everyone_when_tokens_are_empty():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guarded = require_bearer(app, {"slack": "", "copilot": ""})
    assert _guarded_call(guarded, [(b"authorization", b"Bearer ")]) == 401
    assert _guarded_call(guarded, [(b"x-api-key", b"")]) == 401


@pytest.fixture
def served(monkeypatch):
    """The real ASGI app with known tokens, a stub assistant, and reminders that never touch Resend."""
    monkeypatch.setenv("MCP_TOKEN", "slack-token")
    monkeypatch.setenv("COPILOT_MCP_TOKEN", "copilot-token")
    monkeypatch.setenv("REMIND_TOKEN", "remind-token")
    get_settings.cache_clear()
    sessions = []

    class StubAssistant:
        def ask(self, question, session="default"):
            sessions.append(session)
            return AssistantResponse(answer=f"answer to {question}", route="clause_search")

    monkeypatch.setattr(mcp_server, "_assistant", StubAssistant())
    monkeypatch.setattr(reminders, "run", lambda: {"contracts": 0, "sent": False, "reason": "test"})
    with TestClient(mcp_server.build_app()) as client:
        yield client, sessions
    get_settings.cache_clear()


def _ask(client, headers: dict[str, str]) -> int:
    base = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "ask_contracts", "arguments": {"question": "hi"}},
    }
    response = client.post(
        "/mcp", json=call, headers={**base, **headers, "mcp-protocol-version": "2025-06-18"}
    )
    if response.status_code == 200:
        assert "answer to hi" in response.text
    return response.status_code


def test_each_client_gets_its_own_rate_limit_session(served):
    client, sessions = served
    assert _ask(client, {"authorization": "Bearer slack-token"}) == 200
    assert _ask(client, {"x-api-key": "copilot-token"}) == 200
    assert _ask(client, {"authorization": "Bearer remind-token"}) == 401
    assert sessions == ["mcp-slack", "mcp-copilot"]


def test_remind_task_only_accepts_the_remind_token(served):
    client, _ = served
    assert client.post("/tasks/remind").status_code == 401
    assert client.post("/tasks/remind", headers={"authorization": "Bearer slack-token"}).status_code == 401
    assert client.post("/tasks/remind", headers={"x-api-key": "copilot-token"}).status_code == 401
    ok = client.post("/tasks/remind", headers={"authorization": "Bearer remind-token"})
    assert ok.status_code == 200
    assert ok.json() == {"contracts": 0, "sent": False, "reason": "test"}
    assert client.get("/tasks/remind", headers={"authorization": "Bearer remind-token"}).status_code == 405
    assert client.post("/tasks/unknown").status_code == 404


def test_remind_task_reports_send_errors_as_502(served, monkeypatch):
    client, _ = served
    monkeypatch.setattr(
        reminders, "run", lambda: {"contracts": 1, "sent": False, "error": "Resend returned 403"}
    )
    response = client.post("/tasks/remind", headers={"authorization": "Bearer remind-token"})
    assert response.status_code == 502
