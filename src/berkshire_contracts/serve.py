"""One process for the Fly machine: Gradio, the MCP server, and Caddy in front of both.

Caddy is the only public listener. Gradio and MCP stay on localhost, and they share this
process so the retrieval model is loaded once.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("serve")


def _start_caddy() -> subprocess.Popen[bytes] | None:
    binary = shutil.which("caddy")
    config = ROOT / "Caddyfile"
    if binary is None or not config.exists():
        if os.environ.get("CADDY_REQUIRED") == "1":
            raise SystemExit("caddy is required to serve this image")
        log.warning("caddy not found; Gradio is on 127.0.0.1:7860 and MCP is on 127.0.0.1:8000")
        return None
    return subprocess.Popen([binary, "run", "--config", str(config)])


def _start_gradio() -> None:
    import app as gradio_app

    settings = gradio_app.settings
    gradio_app.demo.queue(default_concurrency_limit=4, max_size=32)
    gradio_app.demo.launch(
        auth=gradio_app._auth(),
        auth_message="Berkshire Partners contract intelligence demo. Use the credentials shared with you.",
        theme=gradio_app.THEME,
        css=gradio_app.CSS,
        allowed_paths=[
            str(settings.pdf_dir),
            str(settings.exports_dir),
            str(settings.db_path),
        ],
        server_name="127.0.0.1",
        server_port=7860,
        prevent_thread_lock=True,
        show_error=False,
    )


def main() -> None:
    import uvicorn

    from berkshire_contracts.mcp_server import build_app

    caddy = _start_caddy()
    try:
        _start_gradio()
        uvicorn.run(build_app(), host="127.0.0.1", port=8000, log_level="info")
    finally:
        if caddy is not None and caddy.poll() is None:
            caddy.terminate()


if __name__ == "__main__":
    main()
