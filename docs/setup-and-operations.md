# Setup and operations

Everything needed to run, rebuild, host and connect the app. The [README](../README.md) has the short version.

## Run locally

You need [uv](https://docs.astral.sh/uv/getting-started/installation/), which installs Python 3.12 if it's
missing. From the unzipped submission folder:

```bash
uv sync                    # virtualenv with all dependencies; a few minutes the first time (PyTorch is large)
cp .env.example .env       # add OPENAI_API_KEY; set APP_USERNAME/APP_PASSWORD to require a login
uv run python app.py       # prints the local URL, normally http://127.0.0.1:7860
```

The first start downloads the retrieval model (about 130 MB) from Hugging Face. Without an OpenAI key, the
overview, explorer and data-quality tabs work fully, and chat shows the most relevant passages instead of a written
answer. Developed and tested on macOS with Apple silicon. The dependencies also publish wheels for x86-64 Linux and
Windows, but those haven't been tested; Intel Macs aren't supported because current PyTorch has no build for them.

The submission includes `data/`: the database, the index, the staged PDFs and the exports. It is generated and
kept out of version control because it contains contract text. `uv run python scripts/package_submission.py` builds
the submission zip (`dist/contract-intelligence.zip`) and refuses to include `.env`, `contracts.zip`, the brief or
any secret value set in `.env`.

## Rebuild from the raw contracts

Put `contracts.zip` in the project root. OCR is an optional dependency group because only the parse step uses it.
Its `tesserocr` wheel bundles Tesseract on macOS 15+ and Linux; elsewhere, `uv sync --group ocr` builds it from
source, which needs Tesseract and its headers installed first (`brew install tesseract`, or
`apt install libtesseract-dev`).

```bash
uv sync --group ocr
uv run contracts setup-ocr     # English LSTM and orientation models into data/tessdata
uv run contracts inventory     # profile all 389 PDFs in the zip
uv run contracts stage         # copy every PDF into data/pdfs
uv run contracts parse         # text layer plus OCR
uv run contracts chunk
uv run contracts extract       # about $4.40 with gpt-5-mini; cached and resumable
uv run contracts index         # downloads the retrieval model on first run
uv run contracts export
uv run contracts evaluate      # about $0.03: ten fresh chat answers plus the AI grader
uv run contracts status        # what is in the database, including total model spend
uv run contracts ask "Which contracts auto-renew?"
uv run python scripts/retrieval_benchmark.py  # compare search modes on the answer key; no model calls
uv run python scripts/package_submission.py   # reviewer zip in dist/
```

## Hosting on Fly

`scripts/deploy_fly.py` publishes the app to `https://berkshire-contracts.fly.dev`. It does not use `fly launch`.
The image includes the database, index and staged PDFs. Secrets stay in Fly and are not printed. One machine
(2 shared CPUs, 4 GB) runs Caddy as the only public listener, which routes `/mcp` and `/tasks/*` to the MCP server
and everything else to the Gradio app; both share one Python process so the retrieval model loads once.

The script writes `MCP_TOKEN`, `COPILOT_MCP_TOKEN` and `REMIND_TOKEN` to `.env` when they are missing.

```bash
uv run python scripts/deploy_fly.py
```

## Slack (Claude Tag)

`MCP_TOKEN` in `.env` is the bearer token for `https://berkshire-contracts.fly.dev/mcp`, which Claude Tag calls.
The `claude-tag/` folder is the plugin to attach in the Claude Tag admin console for the Slack workspace.

## Microsoft Teams (Copilot Studio)

Teams uses the same `/mcp` endpoint with its own token, so Slack and Teams can be revoked separately and each gets
its own rate limit. In [Copilot Studio](https://copilotstudio.microsoft.com):

1. Create an agent. Paste [`copilot-studio/instructions.md`](../copilot-studio/instructions.md) into its
   instructions.
2. Under **Tools**, add a tool, choose **Model Context Protocol** (MCP server), and set the server URL to
   `https://berkshire-contracts.fly.dev/mcp`.
3. For authentication choose **API key**, type **Header**, header name `X-API-Key`. When asked to create the
   connection, paste `COPILOT_MCP_TOKEN` from `.env`. The server also accepts `Authorization: Bearer <token>`.
4. Test in the Copilot Studio test pane (for example "Which contracts expire in 2026?"), then **Publish** and add
   the **Microsoft Teams** channel. A Teams admin may need to approve the app before it shows up for others.

## Daily expiry email

A GitHub Actions cron ([`.github/workflows/contract-reminders.yml`](../.github/workflows/contract-reminders.yml))
calls `POST https://berkshire-contracts.fly.dev/tasks/remind` at 13:00 UTC. The Fly app holds the database, so it
builds one email listing every contract whose latest expiration is within `REMINDER_DAYS` (14), and sends it
through [Resend](https://resend.com). With nothing due it still sends a short "nothing expires" note, so a silent
day means the job did not run. Each line shows vendor, contract number, the "act by" date when a notice period is
stated, and the cited file and page.

1. Add the repository secret: `gh secret set REMIND_TOKEN` and paste `REMIND_TOKEN` from `.env`.
2. When ready to send, add `RESEND_API_KEY`, `REMINDER_FROM` (a sender on a domain verified in Resend) and
   `REMINDER_TO` (comma-separated) to `.env`, then redeploy. Until then the endpoint answers `"sent": false` with
   the reason.
3. Test from the repo's **Actions** tab with **Run workflow**. The same day's reruns reuse a Resend idempotency
   key, so they do not send a second copy.

Preview locally with `uv run contracts remind`; add `--send` to send it. GitHub pauses scheduled workflows after 60
days without repository activity, so re-enable it from the Actions tab if that happens.

## Configuration

Settings come from environment variables or `.env`; defaults are in
[`src/berkshire_contracts/config.py`](../src/berkshire_contracts/config.py).

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | — | Extraction, chat answers and judged evaluation (without it, chat shows passages) |
| `APP_USERNAME`, `APP_PASSWORD` | unset | App login (no login screen when unset) |
| `MCP_TOKEN` | unset | Bearer token for the Fly `/mcp` endpoint (Claude Tag) |
| `COPILOT_MCP_TOKEN` | unset | Token for the same endpoint from Copilot Studio (`X-API-Key` header) |
| `REMIND_TOKEN` | unset | Bearer token for `POST /tasks/remind`, called by the GitHub Actions cron |
| `RESEND_API_KEY` | unset | Resend key for the daily expiry email (built but not sent when unset) |
| `REMINDER_FROM`, `REMINDER_TO` | unset | Sender on a Resend-verified domain; comma-separated recipients |
| `REMINDER_DAYS`, `REMINDER_TIMEZONE` | 14, `America/New_York` | Look-ahead window and the time zone for "today" |
| `APP_URL` | `https://berkshire-contracts.fly.dev` | Link in the reminder email |
| `EXTRACTION_MODEL`, `ESCALATION_MODEL`, `ROUTER_MODEL`, `CHAT_MODEL`, `JUDGE_MODEL` | `gpt-5-mini` | Model for each step |
| `MAX_QUESTIONS_PER_MINUTE` | 6 | Per-session rate limit |
| `DAILY_LLM_BUDGET_USD` | 5 | Daily cap on chat spend |
| `LLM_RATE_LIMITS` | per model | Client-side requests and tokens per minute, matched to the OpenAI tier |
| `RETRIEVAL_MODE` | `plaid_rerank` | `plaid_rerank`, `plaid`, `exact` or `bm25` |
| `COLBERT_DEVICE` | auto | Force `cpu`, `cuda` or `mps` for the retrieval model |
| `EVAL_DIR` | `eval` | Point evaluation at another answer key, e.g. the local `eval/holdout` |

## Cost

Building and evaluating everything cost **$4.64** in OpenAI usage across 600 API calls (cache hits excluded):

| Item | Cost |
|---|---|
| Extraction of 389 documents, including escalations | $4.41 |
| AI grader calls during evaluation | $0.11 |
| Routing and answers across all testing | $0.11 |

A typical question costs about $0.0015. `uv run contracts status` and the app's data-quality tab show live totals.

## Repository layout

```
app.py                         Gradio app (four tabs)
src/berkshire_contracts/
  cli.py                       `contracts` pipeline commands
  config.py                    settings from the environment or .env, model prices
  db.py                        SQLite schema (SQLAlchemy)
  schemas.py                   Pydantic schemas for model outputs and query plans
  ingestion/                   inventory, staging, PDF repair, parsing and OCR, chunking
  extraction/                  field definitions, prompts, OpenAI client (structured outputs, cache, cost), extractor
  retrieval/                   late-interaction encoder; PLAID, exact MaxSim and BM25 search
  chat/                        router, deterministic filters, grounded answers, assistant service
  evaluation/                  answer-key loading, scoring, grading sheet, report, retrieval benchmark
  app_support.py               overview charts, page rendering with highlights
  exports.py                   CSV and XLSX exports
  mcp_server.py                /mcp endpoint (ask_contracts) and POST /tasks/remind
  reminders.py                 daily expiry email (Resend)
eval/ground_truth/             human-checked answer key
eval/results/                  report.md, summary.json, per-case CSVs, grading sheet, retrieval benchmark
scripts/                       packaging, Fly deploy, evaluation sampling, retrieval benchmark
claude-tag/                    plugin and skill for Claude Tag in Slack
copilot-studio/                agent instructions for Copilot Studio in Teams
.github/workflows/             daily expiry email cron
tests/                         offline unit tests (throwaway database, no model calls or downloads)
data/                          generated, not in version control: database, index, staged PDFs, exports
```

Run the tests with `uv run pytest`.

## Troubleshooting

- **`ModuleNotFoundError: berkshire_contracts` on macOS.** The editable install's `.pth` file can end up with the
  hidden file flag, and Python skips hidden `.pth` files. Either prefix commands with `PYTHONPATH=src` or clear the
  flag with `chflags nohidden .venv/lib/python3.12/site-packages/*.pth`.
- **The first question is slow.** The retrieval model loads on first use; the app warms it up at start.
- **OpenAI 429 errors.** Lower `LLM_RATE_LIMITS` to your account's tier; calls retry with backoff.
- **No network for Hugging Face.** Once the retrieval model is downloaded, set `HF_HUB_OFFLINE=1` to use the cache.
