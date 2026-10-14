# Retrieval benchmark

Does each search mode return the labeled page among its top results? No model calls; rerun with `uv run python scripts/retrieval_benchmark.py`.

- **Corpus-wide clause questions:** the clause and hybrid chat questions, searched across every document. A hit is any page in the answer key.
- **Page within a document:** for each extraction label with a page, the field's search phrase from `fields.py`, searched only inside that document.
- `top_1` and `top_8`: how often the right page is first, or anywhere in the 8 passages the answer model would see. `mrr`: average of 1/rank of the first right page (1.0 is perfect).

| set                          | mode         |   queries | top_1   | top_8   |   mrr |   median_ms |
|:-----------------------------|:-------------|----------:|:--------|:--------|------:|------------:|
| corpus-wide clause questions | plaid_rerank |         5 | 4/5     | 4/5     | 0.8   |       220.1 |
| corpus-wide clause questions | plaid        |         5 | 4/5     | 4/5     | 0.8   |       200.2 |
| corpus-wide clause questions | exact        |         5 | 4/5     | 4/5     | 0.8   |       213.2 |
| corpus-wide clause questions | bm25         |         5 | 2/5     | 4/5     | 0.567 |         0.4 |
| page within a document       | plaid_rerank |        62 | 32/62   | 56/62   | 0.657 |        21.4 |
| page within a document       | plaid        |        62 | 32/62   | 56/62   | 0.655 |        21.2 |
| page within a document       | exact        |        62 | 32/62   | 56/62   | 0.657 |         8.5 |
| page within a document       | bm25         |        62 | 34/62   | 59/62   | 0.702 |         1.7 |

The corpus-wide set has only five questions, so treat it as a sanity check; the per-document set carries most of the evidence.
