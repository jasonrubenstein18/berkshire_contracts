"""Remote MCP server for Claude Tag (Slack) and Copilot Studio (Teams): one tool, ask_contracts.

Both clients call the public /mcp path, each with its own token, so either can be revoked alone. A
token is the only gate, so an empty token never matches rather than leaving the tool open. The same
process serves POST /tasks/remind, the daily expiry email that a GitHub Actions cron triggers with a
third token.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Mapping
from contextvars import ContextVar

from pydantic import SecretStr
from starlette.types import ASGIApp, Receive, Scope, Send

from berkshire_contracts.chat.service import AssistantResponse, ContractAssistant
from berkshire_contracts.config import get_settings

log = logging.getLogger(__name__)

_assistant: ContractAssistant | None = None
_client: ContextVar[str] = ContextVar("mcp_client", default="unknown")


def format_response(response: AssistantResponse) -> str:
    """Plain text Claude can quote: the answer, a markdown table, and the cited pages."""
    parts = [response.answer.strip()]
    if response.table is not None and len(response.table) > 0:
        parts.append(response.table.to_markdown(index=False))
    cited = set(response.cited)
    shown = [source for source in response.sources if source.sid in cited] or response.sources[:5]
    lines = []
    for source in shown:
        section = f", {source.section}" if source.section else ""
        lines.append(f"- [{source.sid}] {source.filename}, p.{source.page}{section}")
    if lines:
        parts.append("Sources:\n" + "\n".join(lines))
    if response.notes:
        parts.append("\n".join(response.notes))
    return "\n\n".join(part for part in parts if part)


def _presented_token(scope: Scope) -> bytes:
    """`Authorization: Bearer <token>` if sent, else `X-API-Key: <token>` (Copilot Studio's API key header)."""
    bearer = b""
    api_key = b""
    for key, value in scope.get("headers", []):
        name = key.lower()
        if name == b"authorization" and value.startswith(b"Bearer "):
            bearer = value[len(b"Bearer ") :]
        elif name == b"x-api-key":
            api_key = value
    return bearer or api_key


def match_client(scope: Scope, tokens: Mapping[str, str]) -> str | None:
    """Name of the client whose token was presented, or None."""
    presented = _presented_token(scope)
    if not presented:
        return None
    matched = None
    for name, token in tokens.items():
        if token and secrets.compare_digest(presented, token.encode()):
            matched = name
    return matched


async def _respond(send: Send, status: int, body: bytes, content_type: bytes = b"text/plain") -> None:
    headers = [(b"content-type", content_type)]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def require_bearer(app: ASGIApp, tokens: Mapping[str, str]) -> ASGIApp:
    """Reject any request that does not present one of `tokens`; remember which client it was."""

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        client = match_client(scope, tokens)
        if client is None:
            await _respond(send, 401, b"unauthorized")
            return
        reset = _client.set(client)
        try:
            await app(scope, receive, send)
        finally:
            _client.reset(reset)

    return middleware


def current_client() -> str:
    return _client.get()


def ask_contracts(question: str) -> str:
    """Answer a question about these contracts.

    Use this for any question about these contracts: which ones match a condition, what a
    named contract says, dates, amounts, renewal, termination, or liability. The reply cites
    the file name and page. Say when the documents do not answer.
    """
    global _assistant
    if _assistant is None:
        _assistant = ContractAssistant()
    return format_response(_assistant.ask(question, session=f"mcp-{current_client()}"))


def remind_app(token: str) -> ASGIApp:
    """POST runs the daily expiry email and returns its summary as JSON: 200, or 502 if sending failed."""

    async def handler(scope: Scope, receive: Receive, send: Send) -> None:
        import anyio

        from berkshire_contracts import reminders

        if scope.get("method") != "POST":
            await _respond(send, 405, b"method not allowed")
            return
        try:
            result = await anyio.to_thread.run_sync(reminders.run)
        except Exception:
            log.exception("reminder run failed")
            result = {"sent": False, "error": "reminder run failed"}
        status = 502 if result.get("error") else 200
        await _respond(send, status, json.dumps(result).encode(), b"application/json")

    return require_bearer(handler, {"remind": token})


def route_tasks(mcp_app: ASGIApp, tasks: Mapping[str, ASGIApp]) -> ASGIApp:
    """Send /tasks/* to its handler (404 if unknown); everything else, lifespan included, to MCP."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path.startswith("/tasks/"):
            handler = tasks.get(path.rstrip("/"))
            if handler is None:
                await _respond(send, 404, b"not found")
                return
            await handler(scope, receive, send)
            return
        await mcp_app(scope, receive, send)

    return app


def _secret(value: SecretStr | None) -> str:
    return value.get_secret_value().strip() if value is not None else ""


def build_app() -> ASGIApp:
    """Streamable HTTP MCP app on /mcp behind the client tokens, plus POST /tasks/remind."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    settings = get_settings()
    tokens = {"slack": _secret(settings.mcp_token), "copilot": _secret(settings.copilot_mcp_token)}
    if not any(tokens.values()):
        log.warning("MCP_TOKEN and COPILOT_MCP_TOKEN are not set: every /mcp request will be rejected")
    remind_token = _secret(settings.remind_token)
    if not remind_token:
        log.warning("REMIND_TOKEN is not set: every /tasks/remind request will be rejected")

    server = MCPServer("contract-intelligence")
    server.tool(structured_output=False)(ask_contracts)
    mcp_app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return route_tasks(require_bearer(mcp_app, tokens), {"/tasks/remind": remind_app(remind_token)})


def main() -> None:
    import uvicorn

    uvicorn.run(build_app(), host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
