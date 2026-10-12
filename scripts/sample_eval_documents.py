"""Draw the extraction test documents: a seeded random sample within each document type.

Strata use the file-name document type recorded at inventory, before any extraction, so the sample does not
depend on the pipeline's output. Exact duplicates, documents over MAX_PAGES and every document used in an
earlier test set are excluded.

    uv run python scripts/sample_eval_documents.py
"""

from __future__ import annotations

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


def main() -> None:
    con = sqlite3.connect(f"file:{get_settings().db_path}?mode=ro", uri=True)
    files = con.execute(
        "select file_id, filename, heuristic_doc_type, text_profile, page_count from corpus_files "
        "where duplicate_group is null and page_count <= ? order by file_id",
        (MAX_PAGES,),
    ).fetchall()
    auto_renew = {
        fid for fid, text in con.execute("select file_id, text from pages") if AUTO_RENEW.search(text)
    }
    rng = random.Random(SEED)
    chosen: list[tuple[str, tuple]] = []
    taken = set(PREVIOUS_TEST_FILE_IDS)
    for label, types, count, condition in STRATA:
        pool = [
            f
            for f in files
            if f[2] in types
            and f[0] not in taken
            and (condition != "auto_renew" or f[0] in auto_renew)
            and (condition != "scanned" or f[3] == "scanned")
        ]
        for row in rng.sample(pool, count):
            taken.add(row[0])
            chosen.append((label, row))
    for label, (fid, filename, _, profile, pages) in chosen:
        print(f"{fid}  {label:<32} {profile:<8} {pages:>3} pp  {filename}")


if __name__ == "__main__":
    main()
