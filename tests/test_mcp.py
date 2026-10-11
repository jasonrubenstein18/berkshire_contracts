import asyncio

import pandas as pd

from berkshire_contracts.chat.service import AssistantResponse, Source
from berkshire_contracts.mcp_server import format_response, require_bearer


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


def test_bearer_rejects_missing_and_wrong_tokens():
    calls = {"n": 0}

    async def app(scope, receive, send):
        calls["n"] += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guarded = require_bearer(app, "secret-token")

    async def call(header: bytes | None) -> int:
        status = {}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]

        headers = [] if header is None else [(b"authorization", header)]
        await guarded({"type": "http", "headers": headers}, receive, send)
        return status["code"]

    assert asyncio.run(call(None)) == 401
    assert asyncio.run(call(b"Bearer wrong")) == 401
    assert asyncio.run(call(b"Bearer secret-token")) == 200
    assert calls["n"] == 1


def test_bearer_rejects_everyone_when_token_is_empty():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    guarded = require_bearer(app, "")
    status = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]

    asyncio.run(guarded({"type": "http", "headers": [(b"authorization", b"Bearer ")]}, receive, send))
    assert status["code"] == 401
