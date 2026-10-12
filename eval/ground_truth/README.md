# Test sets

| File | Rows | What it labels |
|---|---|---|
| `extraction_labels.csv` | 100 | 10 documents x 10 fields: the value the contracts table should hold |
| `chat_cases.csv` | 10 | Questions with reference answers, expected sources, and whether the assistant should decline |
| `auto_renewal_set.csv` | 10 | The documents that answer chat case q01 ("Which contracts have auto-renewal clauses?") |

**How the documents were chosen.** `scripts/sample_eval_documents.py` draws a seeded random sample (seed 2026)
within each document type, using the file-name type recorded at inventory rather than the extracted one. It
excludes exact duplicates, documents over 40 pages (so a reviewer can check each one), and every document used in
an earlier test set.

**How the labels were drafted.** From the page text and page images only, never from the extracted values. Each
label carries the page and a verbatim quote. The values follow the field definitions in
`src/berkshire_contracts/extraction/fields.py`. The auto-renewal set comes from a text search of every page for
automatic-renewal wording, with each hit read by hand.

**Conventions.**

- `value` is the reference answer. `not stated` means the document doesn't state the field, so any value the
  pipeline fills in counts as invented.
- `accept` lists other defensible readings, separated by `;`. `not stated` in `accept` means a blank is also
  acceptable.
- Dates are `YYYY-MM-DD`, amounts are plain numbers, and yes/no fields are `true` or `false`.

**Reviewer columns.** A person verifies every row against the PDF in `data/pdfs/` (not the app's Contract
explorer, which shows the pipeline's answers).

- `reviewer_verdict`: `agree` or `corrected`. The scorer refuses to run while any row is blank.
- `reviewer_value` (extraction) holds the corrected value when the verdict is `corrected`. For a chat case or a
  set row, edit the cells directly and mark the row `corrected`.
- `changed_after_results`: filled in only if a label is changed after the results were seen, with the reason.
  The report counts these.

**Running it.**

1. `uv run contracts evaluate` scores extraction against these labels and runs the chat questions fresh (cached
   model output is never reused). It writes `eval/results/chat_results.csv` and a blank grading sheet,
   `eval/results/chat_grading.csv`.
2. A person fills `human_correct`, `human_supported` and `human_declined` in the grading sheet.
3. `uv run contracts evaluate --report-only` rescores with those grades and rewrites `report.md` and
   `summary.json` without calling any model. One-line causes for failures go in `eval/results/failure_notes.csv`
   (columns `item`, `category`, `cause`; `item` is the case id, or `file_id field` for an extraction label;
   `category` groups failures that share a root cause).
