"""Remote MCP server for Claude Tag: one tool, ask_contracts, behind a bearer token.

Claude Tag calls this over the public /mcp path. The token is the only gate, so an empty
token rejects every request rather than leaving the tool open.
"""

from __future__ import annotations

import logging
import secrets

from starlette.types import ASGIApp, Receive, Scope, Send

from berkshire_contracts.chat.service import AssistantResponse, ContractAssistant
from berkshire_contracts.config import get_settings

log = logging.getLogger(__name__)

_assistant: ContractAssistant | None = None


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


def require_bearer(app: ASGIApp, token: str) -> ASGIApp:
    """Reject any request whose Authorization header is not exactly `Bearer <token>`."""
    expected = token.encode()

    async def middleware(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        header = b""
        for key, value in scope.get("headers", []):
            if key.lower() == b"authorization":
                header = value
                break
        prefix = b"Bearer "
        presented = header[len(prefix) :] if header.startswith(prefix) else b""
        matches = bool(expected) and secrets.compare_digest(presented, expected)
        if not matches:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")],
                }
            )
            await send({"type": "http.response.body", "body": b"unauthorized"})
            return
        await app(scope, receive, send)

    return middleware


def ask_contracts(question: str) -> str:
    """Answer a question about these contracts.

    Use this for any question about these contracts: which ones match a condition, what a
    named contract says, dates, amounts, renewal, termination, or liability. The reply cites
    the file name and page. Say when the documents do not answer.
    """
    global _assistant
    if _assistant is None:
        _assistant = ContractAssistant()
    return format_response(_assistant.ask(question, session="mcp"))


def build_app() -> ASGIApp:
    """Streamable HTTP MCP app on /mcp, wrapped with the bearer check."""
    from mcp.server.mcpserver import MCPServer
    from mcp.server.transport_security import TransportSecuritySettings

    token = ""
    configured = get_settings().mcp_token
    if configured is not None:
        token = configured.get_secret_value().strip()
    if not token:
        log.warning("MCP_TOKEN is not set: every /mcp request will be rejected")

    server = MCPServer("contract-intelligence")
    server.tool(structured_output=False)(ask_contracts)
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return require_bearer(app, token)


def main() -> None:
    import uvicorn

    uvicorn.run(build_app(), host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
