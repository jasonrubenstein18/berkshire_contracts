"""Deploy the app to Fly.io as berkshire-contracts. Does not run `fly launch`.

Stages code, a checkpoint of the database, the index, the staged PDFs, exports and evaluation
results. Never stages .env, contracts.zip, the brief, or presentation/. Secrets are sent to Fly
on stdin and are never printed. MCP_TOKEN is written to .env when it is missing.

    uv run python scripts/deploy_fly.py
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from berkshire_contracts.config import get_settings  # noqa: E402
from berkshire_contracts.db import snapshot_db  # noqa: E402

APP = "berkshire-contracts"
ORG = "jason-rubenstein"
STAGE = ROOT / "build" / "fly"
COPY_FILES = (
    "app.py",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "Dockerfile",
    "Caddyfile",
    "fly.toml",
    ".dockerignore",
)
DATA_DIRS = ("index", "pdfs", "exports")
EVAL_DIRS = ("ground_truth", "results")


def _fly(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["flyctl", *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def _require(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode == 0:
        return
    detail = (result.stderr or result.stdout).strip()
    raise SystemExit(f"{action} failed:\n{detail}")


def stage() -> Path:
    settings = get_settings()
    if not (settings.index_dir / "meta.json").exists():
        raise SystemExit("Build the index first: contracts index")
    if STAGE.exists():
        shutil.rmtree(STAGE)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
    (STAGE / "data").mkdir(parents=True)
    for name in COPY_FILES:
        shutil.copy2(ROOT / name, STAGE / name)
    shutil.copytree(
        ROOT / "src" / "berkshire_contracts", STAGE / "src" / "berkshire_contracts", ignore=ignore
    )
    snapshot_db(STAGE / "data" / "contracts.db")
    for name in DATA_DIRS:
        shutil.copytree(settings.data_dir / name, STAGE / "data" / name, ignore=ignore)
    for name in EVAL_DIRS:
        shutil.copytree(settings.eval_dir / name, STAGE / "eval" / name, ignore=ignore)
    forbidden = [
        path
        for path in STAGE.rglob("*")
        if path.name in {".env", "contracts.zip", "BP Take-Home_vShare.pdf"}
        or path.suffix == ".zip"
        or "presentation" in path.parts
    ]
    if forbidden:
        raise SystemExit(f"refusing to deploy {forbidden}")
    return STAGE


def ensure_mcp_token() -> str:
    """Return the bearer token, generating one into .env when it is missing. Never print it."""
    path = ROOT / ".env"
    text = path.read_text() if path.exists() else ""
    match = re.search(r"^MCP_TOKEN=(.*)$", text, re.MULTILINE)
    current = match.group(1).strip() if match else ""
    if current:
        return current
    token = secrets.token_urlsafe(32)
    if match:
        text = re.sub(r"^MCP_TOKEN=.*$", f"MCP_TOKEN={token}", text, count=1, flags=re.MULTILINE)
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text += f"MCP_TOKEN={token}\n"
    path.write_text(text)
    get_settings.cache_clear()
    return token


def ensure_app() -> None:
    listed = _fly("apps", "list", "--json")
    _require(listed, "fly apps list")
    names = set()
    for app in json.loads(listed.stdout or "[]"):
        names.add(app.get("Name") or app.get("name"))
    if APP in names:
        print(f"using existing app {APP}")
        return
    created = _fly("apps", "create", APP, "--org", ORG)
    _require(created, "fly apps create")


def set_secrets() -> None:
    settings = get_settings()
    key = settings.openai_api_key.get_secret_value().strip() if settings.openai_api_key else ""
    username = (settings.app_username or "").strip()
    password = settings.app_password.get_secret_value().strip() if settings.app_password else ""
    if not key or not username or not password:
        raise SystemExit("OPENAI_API_KEY, APP_USERNAME and APP_PASSWORD must be set in .env")
    token = ensure_mcp_token()
    payload = "".join(
        f"{name}={value}\n"
        for name, value in (
            ("OPENAI_API_KEY", key),
            ("APP_USERNAME", username),
            ("APP_PASSWORD", password),
            ("MCP_TOKEN", token),
        )
    )
    imported = _fly("secrets", "import", "--app", APP, "--stage", input_text=payload)
    _require(imported, "fly secrets import")
    print("MCP_TOKEN is in .env. Copy it into the Claude Tag connector. It was not printed here.")


def deploy() -> None:
    folder = stage()
    ensure_app()
    set_secrets()
    result = subprocess.run(
        ["flyctl", "deploy", "--ha=false", "--config", str(folder / "fly.toml")],
        cwd=folder,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print(f"https://{APP}.fly.dev")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--stage-only", action="store_true", help="build ./build/fly without deploying")
    args = parser.parse_args()
    if args.stage_only:
        print(stage())
        return
    deploy()


if __name__ == "__main__":
    main()
