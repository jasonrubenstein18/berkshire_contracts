import threading
import time
from datetime import date

import numpy as np

from berkshire_contracts.evaluation.compare import deterministic_match, to_comparable
from berkshire_contracts.evaluation.runner import _outcome, _plan_matches
from berkshire_contracts.retrieval import colbert
from berkshire_contracts.retrieval.colbert import maxsim, maxsim_many, maxsim_segments


def unit_rows(rng, n, dim=8):
    x = rng.normal(size=(n, dim)).astype(np.float32)
    return x / np.linalg.norm(x, axis=1, keepdims=True)


def test_maxsim_matches_brute_force():
    rng = np.random.default_rng(0)
    query, doc = unit_rows(rng, 4), unit_rows(rng, 11)
    expected = sum(max(float(q @ d) for d in doc) for q in query)
    assert np.isclose(maxsim(query, doc), expected, atol=1e-5)


def test_segment_maxsim_equals_per_document_maxsim():
    rng = np.random.default_rng(1)
    query = unit_rows(rng, 5)
    docs = [unit_rows(rng, n) for n in (3, 9, 1, 6)]
    stacked = np.concatenate(docs)
    offsets = np.cumsum([0, *[len(d) for d in docs]])
    assert np.allclose(maxsim_segments(query, stacked, offsets), maxsim_many(query, docs), atol=1e-5)
    assert np.allclose(maxsim_many(query, docs), [maxsim(query, d) for d in docs], atol=1e-5)


def test_concurrent_first_calls_load_one_encoder(monkeypatch):
    built = []

    class SlowEncoder:
        def __init__(self):
            time.sleep(0.2)
            built.append(self)

    monkeypatch.setattr(colbert, "LateInteractionEncoder", SlowEncoder)
    colbert._cached_encoder.cache_clear()
    results = []
    threads = [threading.Thread(target=lambda: results.append(colbert.get_encoder())) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    colbert._cached_encoder.cache_clear()
    assert len(built) == 1
    assert all(r is built[0] for r in results)


def test_field_comparators():
    assert deterministic_match("award_amount_usd", 1_000_400.0, 1_000_000.0)
    assert not deterministic_match("award_amount_usd", 1_100_000.0, 1_000_000.0)
    assert deterministic_match("vendor_name", "DLR Group Inc.", "DLR Group")
    assert deterministic_match("vendor_name", "Tyler Technologies, Inc.", "Tyler Technologies")
    assert not deterministic_match("vendor_name", "Motorola Solutions", "Tyler Technologies")
    assert deterministic_match("contract_number", "SOW 141301", "141301")
    assert deterministic_match("governing_law", "State of Illinois", "Illinois")
    assert deterministic_match("effective_date", "2025-04-04", "2025-04-04")
    assert deterministic_match("liability_cap", "x", "y") is None
    assert to_comparable("effective_date", date(2025, 4, 4)) == "2025-04-04"


def test_outcomes():
    assert _outcome(False, False, None, None, []) == "correct_null"
    assert _outcome(False, False, None, "made up", []) == "false_positive"
    assert _outcome(False, False, "x", None, [None]) == "acceptable_null"
    assert _outcome(False, False, "x", None, []) == "miss"
    assert _outcome(True, False, "x", "x", []) == "correct"
    assert _outcome(False, True, "x", "y", ["y"]) == "correct_alt"
    assert _outcome(False, False, "x", "z", []) == "wrong"


def test_plan_check():
    plan = {
        "metric": "list",
        "filters": [
            {
                "field": "expiration_date",
                "op": "between",
                "value": "2026-01-01",
                "value_to": "2026-12-31",
                "values": [],
            }
        ],
    }
    expected = {
        "filter_field": "expiration_date",
        "filter_value": "2026-01-01",
        "filter_value_to": "2026-12-31",
    }
    assert _plan_matches(expected, plan)
    assert not _plan_matches(expected | {"metric": "count"}, plan)
    assert not _plan_matches(expected, None)
