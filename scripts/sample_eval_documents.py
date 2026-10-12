"""Draw the extraction test documents: a seeded random sample within each document type.

Strata use the file-name document type recorded at inventory, before any extraction, so the sample does not
depend on the pipeline's output. Exact duplicates, documents over MAX_PAGES and every document used in an
earlier test set are excluded.

    uv run python scripts/sample_eval_documents.py
    uv run python scripts/sample_eval_documents.py --seed 2027 --exclude-tested

`--exclude-tested` draws an out-of-sample set: it also drops every document in eval/ground_truth, the documents
re-extracted while fixing those cases, and every document in the same contract family as any of them. A stratum
left too small is filled from FALLBACK_TYPES.
"""

from __future__ import annotations

import argparse
import csv
import random
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from berkshire_contracts.config import get_settings  # noqa: E402

SEED = 2026
MAX_PAGES = 40
AUTO_RENEW = re.compile(
    r"automatic(?:ally)?\s+renew|renew(?:ed|s)?\s+automatically|auto[- ]?renew|evergreen", re.IGNORECASE
)
PREVIOUS_TEST_FILE_IDS = {
    "532a78eb47d6",
    "065d16803269",
    "d7fa2b1aa526",
    "63032717dd43",
    "a88a2fbbaf1d",
    "3be3d76f3183",
    "a21f680ab3d9",
    "45077a43180c",
    "37ef9dac1cb9",
    "414aa6ac10c0",
    "47450092d658",
    "1ad3db68c1bc",
    "76de4877b8c0",
    "5a57b7dfcf16",
    "dea4a934f7b5",
    "7e04f03b3d6b",
    "cee0dfec2d6d",
    "20fd2a28bab3",
    "6c4dcbc8e8c6",
    "84c3ea65ac55",
    "fa941bc78078",
    "f5e2758b71e7",
}
REEXTRACTED_FILE_IDS = {"76de4877b8c0", "be4d3e3e29bd", "9cf24b6c063b"}
TESTED_DIR = ROOT / "eval" / "ground_truth"
FALLBACK_TYPES = {"agreement"}
# (label, heuristic document types, count, extra condition)
STRATA = (
    ("agreement, auto-renewal wording", {"agreement"}, 1, "auto_renew"),
    ("agreement, scanned", {"agreement"}, 1, "scanned"),
    ("agreement", {"agreement"}, 1, None),
    ("renewal / extension", {"renewal_extension"}, 2, None),
    ("amendment / modification", {"amendment_modification"}, 1, None),
    ("award notice", {"award_notice"}, 1, None),
    ("vendor disclosure", {"vendor_disclosure"}, 1, None),
    ("lease", {"lease"}, 1, None),
    ("task order or price adjustment", {"task_order_sow", "price_adjustment"}, 1, None),
)


def _csv_rows(name: str) -> list[dict[str, str]]:
    with (TESTED_DIR / name).open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def tested_file_ids() -> set[str]:
    ids = {r["file_id"] for r in _csv_rows("extraction_labels.csv")}
    ids |= {r["file_id"] for r in _csv_rows("auto_renewal_set.csv")}
    for r in _csv_rows("chat_cases.csv"):
        for column in ("expected_file_ids", "optional_file_ids", "expected_pages"):
            for item in r[column].split(";"):
                file_id = item.strip().partition(":")[0]
                if file_id and not file_id.startswith("@"):
                    ids.add(file_id)
    return ids | REEXTRACTED_FILE_IDS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=SEED)
    parser.add_argument("--exclude-tested", action="store_true")
    args = parser.parse_args()

    con = sqlite3.connect(f"file:{get_settings().db_path}?mode=ro", uri=True)
    files = con.execute(
        "select file_id, filename, heuristic_doc_type, text_profile, page_count from corpus_files "
        "where duplicate_group is null and page_count <= ? order by file_id",
        (MAX_PAGES,),
    ).fetchall()
    auto_renew = {
        fid for fid, text in con.execute("select file_id, text from pages") if AUTO_RENEW.search(text)
    }
    taken = set(PREVIOUS_TEST_FILE_IDS)
    if args.exclude_tested:
        tested = tested_file_ids() | PREVIOUS_TEST_FILE_IDS
        marks = ",".join("?" * len(tested))
        families = {
            f
            for (f,) in con.execute(
                f"select distinct family_id from corpus_files where file_id in ({marks})", sorted(tested)
            )
            if f
        }
        family_of = dict(con.execute("select file_id, family_id from corpus_files"))
        taken |= tested | {fid for fid, fam in family_of.items() if fam in families}
        print(f"excluded {len(taken)} documents ({len(families)} contract families)")
    rng = random.Random(args.seed)
    chosen: list[tuple[str, tuple]] = []
    for label, types, count, condition in STRATA:
        pool = [
            f
            for f in files
            if f[2] in types
            and f[0] not in taken
            and (condition != "auto_renew" or f[0] in auto_renew)
            and (condition != "scanned" or f[3] == "scanned")
        ]
        if len(pool) < count:
            print(f"{label}: only {len(pool)} eligible, filling from {', '.join(sorted(FALLBACK_TYPES))}")
            label = f"{label} (filled from {', '.join(sorted(FALLBACK_TYPES))})"
            pool += [f for f in files if f[2] in FALLBACK_TYPES and f[0] not in taken and f not in pool]
        for row in rng.sample(pool, count):
            taken.add(row[0])
            chosen.append((label, row))
    for label, (fid, filename, _, profile, pages) in chosen:
        print(f"{fid}  {label:<32} {profile:<8} {pages:>3} pp  {filename}")


if __name__ == "__main__":
    main()
