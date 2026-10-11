"""Publish the app to a protected Hugging Face Space (protected visibility needs a Hugging Face PRO account).

Uploads code, the SQLite database, the late-interaction index, the 100 sampled PDFs, exports and evaluation
results. Never uploads contracts.zip, .env, the brief, the LLM cache or OCR scratch files. Secrets are set as
Space secrets from the local .env and are never printed.

    .venv/bin/python scripts/deploy_space.py --repo-id <user>/contract-intelligence
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from huggingface_hub import HfApi  # noqa: E402

from berkshire_contracts.config import get_settings  # noqa: E402
from berkshire_contracts.db import snapshot_db  # noqa: E402

STAGE = ROOT / "build" / "space"

REQUIREMENTS = """\
--extra-index-url https://download.pytorch.org/whl/cpu
torch==2.11.0
fast-plaid==1.4.6.2110
pylate==1.6.0
bm25s==0.3.13
PyStemmer==3.1.0
openai==3.28.0
pydantic==2.14.0
pydantic-settings==2.15.0
SQLAlchemy==2.1.4
pandas==3.0.6
numpy==2.5.4
plotly==7.1.0
PyMuPDF==1.28.2
rapidfuzz==3.14.6
tenacity==9.2.1
rich==15.0.0
typer==0.27.3
XlsxWriter==3.2.9
tabulate==0.10.0
python-dotenv==1.2.4
python-dateutil>=2.9
"""

SPACE_CARD = """\
---
title: Contract Intelligence PoC
colorFrom: blue
colorTo: gray
sdk: gradio
sdk_version: 6.30.0
python_version: "3.12"
app_file: app.py
pinned: false
short_description: Ask questions of 100 Lake County procurement contracts
---

# Contract Intelligence PoC

A deal-team assistant over a 100-document sample of Lake County, Illinois procurement contracts:
evidence-backed field extraction into SQLite, late-interaction (ColBERT) clause retrieval, and grounded,
cited answers. Sign in with the credentials shared with you. Source code and documentation are in the
project repository's README.
"""


def stage() -> Path:
    settings = get_settings()
    if not (settings.index_dir / "meta.json").exists():
        raise SystemExit("Build the index first: contracts index")
    if STAGE.exists():
        shutil.rmtree(STAGE)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
    (STAGE / "data").mkdir(parents=True)
    shutil.copy2(ROOT / "app.py", STAGE / "app.py")
    shutil.copytree(
        ROOT / "src" / "berkshire_contracts", STAGE / "src" / "berkshire_contracts", ignore=ignore
    )
    snapshot_db(STAGE / "data" / settings.db_path.name)
    for name in ("index", "sample_pdfs", "exports"):
        shutil.copytree(settings.data_dir / name, STAGE / "data" / name, ignore=ignore)
    for name in ("results", "ground_truth"):
        shutil.copytree(settings.eval_dir / name, STAGE / "eval" / name, ignore=ignore)
    (STAGE / "requirements.txt").write_text(REQUIREMENTS)
    (STAGE / "README.md").write_text(SPACE_CARD)
    forbidden = [p for p in STAGE.rglob("*") if p.name in {".env", "contracts.zip"} or p.suffix == ".zip"]
    if forbidden:
        raise SystemExit(f"refusing to upload {forbidden}")
    return STAGE


def deploy(repo_id: str, hardware: str) -> None:
    settings = get_settings()
    token = settings.hf_token.get_secret_value() if settings.hf_token else None
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else None
    if not token or not key:
        raise SystemExit("HF_TOKEN and OPENAI_API_KEY must be set in .env")
    username, password = settings.app_username, settings.app_password
    if not username or not password:
        raise SystemExit("Set APP_USERNAME and APP_PASSWORD in .env so the Space is not open to anyone")

    folder = stage()
    api = HfApi(token=token)
    api.create_repo(
        repo_id,
        repo_type="space",
        space_sdk="gradio",
        visibility="protected",
        exist_ok=True,
        space_hardware=hardware,
    )
    secrets = {"OPENAI_API_KEY": key, "APP_USERNAME": username, "APP_PASSWORD": password.get_secret_value()}
    for name, value in secrets.items():
        api.add_space_secret(repo_id, name, value)
    api.add_space_variable(repo_id, "COLBERT_DEVICE", "cpu")
    api.add_space_variable(repo_id, "GRADIO_SERVER_NAME", "0.0.0.0")
    api.upload_folder(
        repo_id=repo_id,
        repo_type="space",
        folder_path=folder,
        commit_message="Deploy contract intelligence PoC",
        delete_patterns=["*"],
    )
    print(f"uploaded {sum(1 for p in folder.rglob('*') if p.is_file())} files; waiting for the build…")
    runtime = api.wait_for_space(repo_id, timeout=1800, poll_interval=15)
    print(f"stage: {runtime.stage}  https://huggingface.co/spaces/{repo_id}")
    if runtime.stage != "RUNNING":
        for line in list(api.fetch_space_logs(repo_id, build=True))[-40:]:
            print(line)
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--repo-id", required=True, help="e.g. your-username/contract-intelligence")
    parser.add_argument("--hardware", default="cpu-basic")
    parser.add_argument("--stage-only", action="store_true", help="build ./build/space without uploading")
    args = parser.parse_args()
    if args.stage_only:
        print(stage())
        return
    deploy(args.repo_id, args.hardware)


if __name__ == "__main__":
    main()
