# How it works, in detail

The technical companion to the [README](../README.md): the database, the pipeline, design choices, failure
handling, how quality is measured, and how it would be monitored and secured.

## The database

`data/contracts.db` is SQLite and opens in any SQLite browser or with `sqlite3`. Every value can be traced back to
its source.

| Table | Rows | Contents |
|---|---|---|
| `contracts` | 389 | One row per file: the 19 fields, parse and extraction status, review flags, model and prompt version |
| `field_evidence` | 7,391 | One row per file and field: value, verbatim quote, page, confidence, whether the quote was found in the source |
| `pages` | 4,530 | Page text, whether it came from the text layer or OCR, OCR confidence |
| `chunks` | 12,825 | Retrieval passages with page and section heading |
| `corpus_files` | 389 | Inventory of the whole archive: text profile, encryption, contract family, duplicates |
| `llm_calls` | — | Every model call: purpose, model, prompt version, tokens, cost, latency, cache hit |
| `chat_logs` | — | Every question: route, query plan, retrieved passages, answer |
| `pipeline_runs` | — | Every pipeline step with status and statistics |

The same data is exported to `data/exports/contracts.csv`, `contracts.xlsx` (with a data dictionary) and
`field_evidence.csv`. Field definitions and their business use are in
[`src/berkshire_contracts/extraction/fields.py`](../src/berkshire_contracts/extraction/fields.py).

## Offline pipeline

Every step is resumable and logged to `pipeline_runs`.

1. **Inventory**: profile all 389 PDFs inside the zip: text layer versus scan, encryption, upload wrappers,
   malformed files, exact duplicates, and contract families inferred from file-name contract numbers.
2. **Stage**: copy every PDF out of the zip into `data/pdfs`, stripping the upload wrapper (bytes before the PDF
   header). 389 files, 381 unique documents, 8 exact duplicates.
3. **Parse**: read the PyMuPDF text layer first, then run Tesseract OCR (300 dpi, orientation detection) on pages
   with little or garbled text. 1,648 of the 4,530 pages were OCR'd. 22 documents parsed partially and were flagged;
   none failed.
4. **Chunk**: page-bounded, heading-aware passages (at most 230 tokens, 40 overlapping).
5. **Extract**: one structured-output call per document (gpt-5-mini). Documents over 160k characters get their
   first and last pages plus the pages that best match each field. Each field returns a status, value, verbatim
   quote, page and confidence. Quotes are checked against the page text, dates and renewal terms are checked for
   consistency, and flagged fields are re-read once with more reasoning effort (25 documents). Exact duplicates copy
   their twin's values. 32 documents carry review flags.
6. **Index**: encode passages with `lightonai/answerai-colbert-small-v1` (one 96-dimensional vector per token;
   2.1 million vectors, 657 MB). Build a FastPLAID index for candidate search and a BM25 index for comparison.
7. **Export** the CSV and XLSX files, then **evaluate**.

## Answering a question

- **Router** (gpt-5-mini, low reasoning effort) returns a validated `QueryPlan`: the route, filters on an allowlist
  of fields and operators, grouping, metric, named contracts or vendors, and a search query. If the router fails,
  the app searches the text directly.
- **Data cuts**: deterministic filters over the `contracts` table. Award totals are de-duplicated by contract family
  and vendor. Documents that don't state a field are reported as unknown, never counted as "no". A named contract
  number outranks a vendor name, and a vendor name matches only when all its words match. If a named vendor or
  contract isn't in the extracted data, the app searches only the documents that mention it; if none does, it says
  so without calling the model.
- **Clause questions**: PLAID returns 200 candidates, re-scored with exact MaxSim. The top 8 passages (capped per
  file) pass a relevance gate before the answer model sees them. The answer model must use only those sources, cite
  every sentence as `[S#]`, and say so when the sources don't answer. Citations are checked against the passages
  actually provided.
- **Hybrid**: filter in the database first, then search clauses within the matching documents.

## Design decisions

- **Late-interaction search.** Contract questions hinge on exact terms, and late interaction compares every query
  word with every passage word, so specific wording isn't averaged away while paraphrases still match. The
  [retrieval benchmark](../eval/results/retrieval_benchmark.md) shows the trade-off honestly: on the five
  corpus-wide clause questions it ranked the right page first 4 times versus 2 for BM25, but for finding a field's
  page inside a known document, keyword search (BM25) did as well or slightly better. The two methods missed
  different questions, which is why hybrid search is on the roadmap.
- **A relevance gate on the question's own words.** Full MaxSim scores include about 25 query-expansion tokens
  that match almost anything, so off-topic questions scored as high as answerable ones. The gate uses the mean best
  match over the question's own tokens, with a threshold (0.88) set below every answerable test question.
- **Evidence before values.** The model quotes the clause and page before giving a value, so every value is
  checkable in one click and quotes are verified automatically (99.8% of 3,775 extracted values verify). A blank is
  preferred to a guess.
- **No generated SQL.** The model only fills a typed plan; code executes it. Counts are reproducible, the database
  can't be injected into, and the plan is logged.
- **Inspectable storage.** SQLite plus CSV/XLSX, so reviewers need no infrastructure.
- **Cheap by design.** gpt-5-mini for every step, a response cache, client-side rate pacing and a daily budget cap.

## When things go wrong

- **Files**: encrypted PDFs are opened with the empty password, upload wrappers are stripped, malformed files are
  repaired. Pages with no usable text are OCR'd; documents that still have gaps are marked `partial` and flagged.
- **Model calls**: retried with backoff and paced under the account's limits. Failures are recorded per document
  without stopping the run.
- **Questions**: empty, overlong (over 600 characters) and too-frequent (over 6 a minute) questions get a clear
  message. Off-topic questions are declined. Weak evidence produces "the documents provided don't cover it".
- **Outages and budget**: if the router fails, the app searches the text directly. If the answer model fails or the
  daily budget is reached, the app shows the most relevant passages instead. Without an API key, the other tabs
  keep working.
- **UI**: stack traces are never shown to users; errors are logged on the server.

## How quality is measured

- **Table extraction** is scored by code, not an AI grader, so the score is reproducible: amounts within 0.5%,
  dates, day counts, yes/no and document type exactly, vendor names ignoring suffixes like "Inc.". Accuracy counts
  correct blanks; precision and recall are reported separately because a wrong value misleads while a blank only
  sends the reader to the PDF; invented values on fields the document doesn't state measure hallucination.
- **Chat** is graded by a person: *correct* and *supported* (every claim backed by a cited page) are graded apart.
  Declines are scored both ways, because a system that refused everything would ace the unanswerable questions.
  Whether the expected page was found separates search failures from answer-writing failures. An AI grader gives a
  blind second opinion, and its agreement with the person shows how far it can be trusted at scale.
- **Retrieval** is scored without any model: whether the labeled page is first, or in the top 8, for each search
  mode (`uv run python scripts/retrieval_benchmark.py`).

The full results are in [`eval/results/report.md`](../eval/results/report.md); earlier runs and what was fixed
between them are kept in `eval/results/run1_before_vendor_fix/` and `run2_before_scope_fix/`.

## Monitoring in production

- **Release gate**: rerun the test set on every prompt, model or parser change, and weekly with no change to catch
  provider-side drift. Block a release that lowers accuracy or declines, or raises invented values. Pin dated model
  snapshots.
- **Weekly drift signals** from `field_evidence`, `llm_calls` and `chat_logs`: per-field fill rate and
  quote-verified rate, OCR share and confidence, document-type mix, top retrieval score, refusal rate, uncited
  answers, route mix, cost and latency. A drop in one field suggests new templates or wording; a drop across fields
  points to the model or prompt.
- **Human audit**: a person grades a weekly sample of real questions, with the AI grader screening more. Answers
  users mark wrong become new test cases.

## Guardrails and confidentiality

Built: evidence on every value; refusal over guessing (relevance gate, insufficient-evidence flag, citation
checks); allowlisted query plans; document text treated as data in every prompt; review flags; login, rate limit,
question length limit and daily budget; audit logs of every question, plan, source and model call. Secrets live
only in `.env`, are loaded as `SecretStr`, and are never logged or packaged. OpenAI calls use `store=false`.

Proposed for production: human sign-off before an extracted value enters an investment committee memo; a second
reviewer for award, termination and liability values; a review queue in the app; SSO with per-deal access;
encryption at rest and retention limits; a zero-data-retention agreement or an in-tenant model endpoint over
private networking.
