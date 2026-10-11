# Evaluation report

Generated 2026-10-11 21:12 UTC · extraction model `gpt-5-mini` · judge `gpt-5-mini` (rubric judge-v2) · retriever `lightonai/answerai-colbert-small-v1`.

Ground truth was labeled from the source text by the AI assistant that built the pipeline, before extraction results were viewed; it has not yet been independently audited. Labels were not edited after results were seen; rubric judge-v2 added field guidance for descriptions after reviewing judge-v1 disagreements.

## Field extraction

- 261 labeled fields across 14 documents (native, OCR, long-form, amendments, renewals, encrypted, upload-wrapper and null-heavy forms).
- Accuracy (value or accepted alternative, including correct blanks): **93%**; strict: 91%.
- Value precision 91%, recall 88%, F1 89%.
- Hallucination rate on fields the document does not state: **3%**.
- Evidence quotes verified verbatim in the source: 100%; cited page exact 97%, within one page 99%.
- Confidence as a review trigger: sending every value below high confidence to review (10% of values) catches 36% of wrong values, so confidence alone is not a sufficient review gate (see the table below).

| field                          |   labels |   accuracy |   value_precision |   value_recall |   hallucination_rate |
|:-------------------------------|---------:|-----------:|------------------:|---------------:|---------------------:|
| Contract #                     |       14 |      1     |             1     |          1     |                0     |
| Document type                  |       14 |      0.929 |             0.929 |          0.929 |                0     |
| Vendor                         |       14 |      0.929 |             1     |          0.929 |                0     |
| Description                    |       14 |      0.929 |             0.923 |          0.923 |                0     |
| Effective date                 |       13 |      0.769 |             0.7   |          0.778 |                0.25  |
| Expiration date                |       13 |      0.769 |             0.625 |          0.833 |                0.286 |
| Renewal type                   |       14 |      1     |             1     |          1     |                0     |
| Renewal options                |       14 |      0.929 |             0.889 |          0.889 |                0     |
| Auto-renewal                   |       14 |      0.929 |             1     |          0.667 |                0     |
| Renewal notice (days)          |       13 |      1     |             1     |          1     |                0     |
| Award amount (USD)             |       13 |      1     |             1     |          1     |                0     |
| Pricing basis                  |       14 |      0.857 |             1     |          0.727 |                0     |
| Termination for convenience    |       14 |      1     |             1     |          1     |                0     |
| Termination notice (days)      |       14 |      1     |             1     |          1     |                0     |
| Liability cap                  |       13 |      0.923 |             0.667 |          0.667 |                0     |
| Indemnification                |       14 |      1     |             1     |          1     |                0     |
| Insurance requirements         |       14 |      0.714 |             0.429 |          0.375 |                0     |
| Governing law                  |       14 |      1     |             1     |          1     |                0     |
| Assignment / change of control |       14 |      1     |             1     |          1     |                0     |

| confidence   |   values |   accuracy |
|:-------------|---------:|-----------:|
| high         |      138 |      0.935 |
| medium       |       15 |      0.733 |
| low          |        1 |      0     |

| category          |   documents |   labels |   accuracy |   value_f1 |
|:------------------|------------:|---------:|-----------:|-----------:|
| agreement         |           5 |       90 |      0.9   |      0.89  |
| amendment         |           2 |       38 |      0.974 |      0.917 |
| award_notice      |           1 |       19 |      0.947 |      0.96  |
| encrypted         |           1 |       19 |      1     |      0.909 |
| form_fields       |           1 |       19 |      0.947 |      0.941 |
| lease             |           1 |       19 |      0.842 |      0.833 |
| long_form         |           2 |       33 |      0.818 |      0.82  |
| malformed         |           1 |       19 |      0.947 |      0.938 |
| mixed             |           3 |       52 |      0.885 |      0.835 |
| native            |           7 |      133 |      0.955 |      0.926 |
| null_heavy        |           1 |       19 |      0.947 |      0.8   |
| ocr               |           4 |       76 |      0.921 |      0.893 |
| partial           |           1 |       19 |      0.842 |      0.833 |
| price_adjustment  |           1 |       19 |      1     |      1     |
| renewal           |           2 |       38 |      0.974 |      0.916 |
| selected_context  |           1 |       14 |      0.857 |      0.857 |
| task_order_sow    |           1 |       19 |      0.895 |      0.8   |
| upload_wrapper    |           1 |       19 |      0.947 |      0.941 |
| vendor_disclosure |           1 |       19 |      0.947 |      0.8   |

## Retrieval

20 paraphrased questions with known answer pages; a hit is a chunk from the right page.

| retriever                                   |   page_recall@1 |   page_recall@5 |   page_recall@10 |   file_recall@5 |   MRR@10 |   median_latency_ms |
|:--------------------------------------------|----------------:|----------------:|-----------------:|----------------:|---------:|--------------------:|
| BM25 (lexical baseline)                     |            0.25 |            0.55 |             0.55 |            0.75 |    0.388 |                 0.3 |
| PLAID only (compressed late interaction)    |            0.25 |            0.6  |             0.6  |            0.75 |    0.36  |               204.8 |
| Exact MaxSim (brute-force late interaction) |            0.3  |            0.6  |             0.6  |            0.75 |    0.393 |               203.3 |
| PLAID + exact MaxSim rerank (deployed)      |            0.3  |            0.6  |             0.6  |            0.75 |    0.393 |               194.4 |

Late interaction (deployed) finds the answer page in the top 5 for 60% of questions versus 55% for BM25 (MRR 0.39 vs 0.39).

Best retriever on page recall@5: PLAID only (compressed late interaction).

## End-to-end chat

- 13 cases (structured, clause lookup, hybrid, negative and out-of-scope).
- Routing accuracy 85%; judge-rated correct 91% (correct or partially correct 91%); grounded 64%.
- Correct abstention on unanswerable questions: 67%; expected source cited: 83%.
- Median latency 2.93s (p90 6.85s); mean cost $0.0014 per question.

| id   | type         | route         | route_ok   | judge_correctness   |   judge_grounded |   abstained_ok |   source_cited |   set_recall |   plan_ok |   latency_s |
|:-----|:-------------|:--------------|:-----------|:--------------------|-----------------:|---------------:|---------------:|-------------:|----------:|------------:|
| c01  | structured   | structured    | True       | correct             |                1 |            nan |            nan |            1 |       nan |        3.77 |
| c02  | structured   | structured    | True       | nan                 |              nan |            nan |            nan |          nan |         1 |        1.96 |
| c03  | structured   | structured    | True       | nan                 |              nan |            nan |            nan |          nan |         1 |        2.89 |
| c04  | clause       | structured    | False      | correct             |                1 |            nan |              1 |          nan |       nan |        2.93 |
| c05  | clause       | clause_search | True       | correct             |                1 |            nan |              1 |          nan |       nan |        6.76 |
| c06  | clause       | structured    | True       | correct             |                1 |            nan |              1 |          nan |       nan |        2.82 |
| c07  | clause       | hybrid        | True       | correct             |                1 |            nan |              1 |          nan |       nan |        6.87 |
| c08  | clause       | clause_search | True       | incorrect           |                0 |            nan |              0 |          nan |       nan |       13.08 |
| c09  | hybrid       | structured    | True       | correct             |                1 |            nan |            nan |          nan |       nan |        3.38 |
| c10  | negative     | clause_search | True       | correct             |                0 |              1 |            nan |          nan |       nan |        6.37 |
| c11  | negative     | structured    | False      | correct             |                0 |              0 |            nan |          nan |       nan |        2.51 |
| c12  | out_of_scope | out_of_scope  | True       | correct             |                0 |              1 |            nan |          nan |       nan |        2.72 |
| c13  | clause       | structured    | True       | correct             |                1 |            nan |              1 |          nan |       nan |        2.49 |
