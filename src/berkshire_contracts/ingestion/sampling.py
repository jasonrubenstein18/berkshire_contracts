"""Deterministic stratified sample of the corpus.

Edge cases that exercise failure handling are always included, two multi-file contract families are kept
together to demonstrate family roll-ups, and the remaining slots are allocated proportionally across
(heuristic document type x text profile) strata with systematic selection along document length, so the
sample spans short letters through long agreements.
"""

from __future__ import annotations

import hashlib
import math
import shutil

import pandas as pd

from berkshire_contracts.config import get_settings
from berkshire_contracts.db import CorpusFile, session_scope
from berkshire_contracts.ingestion.inventory import read_member
from berkshire_contracts.ingestion.pdf import strip_wrapper


def _seeded_unit(seed: str, key: str) -> float:
    digest = hashlib.sha256(f"{seed}:{key}".encode()).hexdigest()
    return int(digest[:12], 16) / float(16**12)


def _text_group(profile: str) -> str:
    return "native" if profile == "native" else "image_heavy"


def edge_case_ids(frame: pd.DataFrame) -> list[tuple[str, str]]:
    picks: list[tuple[str, str]] = []
    for fid in frame.loc[frame.pdf_offset > 0, "file_id"]:
        picks.append((fid, "edge:upload_wrapper"))
    for fid in frame.loc[frame.encrypted.astype(bool), "file_id"]:
        picks.append((fid, "edge:encrypted"))
    repaired = frame.open_notes.fillna("").str.contains("repair", case=False) | frame.open_error.notna()
    for fid in frame.loc[repaired, "file_id"]:
        picks.append((fid, "edge:malformed"))

    groups = frame[frame.duplicate_group.notna()].groupby("duplicate_group")
    ordered = sorted(groups, key=lambda kv: (-len(kv[1]), kv[0]))
    for _, group in ordered[:2]:
        for fid in group.sort_values("filename").file_id:
            picks.append((fid, "edge:exact_duplicate"))

    readable = frame[frame.open_error.isna()]
    longest = readable.sort_values(["native_chars", "filename"], ascending=[False, True]).head(1)
    picks += [(fid, "edge:longest_native_text") for fid in longest.file_id]
    scans = readable[readable.text_profile == "scanned"].sort_values(
        ["page_count", "filename"], ascending=[False, True]
    )
    picks += [(fid, "edge:largest_scan") for fid in scans.head(1).file_id]
    return picks


def family_showcase_ids(frame: pd.DataFrame, families: int = 2, per_family: int = 4) -> list[tuple[str, str]]:
    """Pick the largest families and keep one file of each document type, so a contract's lifecycle is visible."""
    counts = frame[frame.family_id.notna()].groupby("family_id").size().sort_values(ascending=False)
    picks: list[tuple[str, str]] = []
    for family in counts.head(families).index:
        members = frame[frame.family_id == family].sort_values("filename")
        chosen = members.drop_duplicates("heuristic_doc_type").head(per_family)
        picks += [(fid, f"family:{family}") for fid in chosen.file_id]
    return picks


def allocate(sizes: dict[str, int], total: int) -> dict[str, int]:
    """Largest-remainder proportional allocation with at least one slot per non-empty stratum."""
    population = sum(sizes.values())
    if population == 0 or total <= 0:
        return dict.fromkeys(sizes, 0)
    raw = {k: total * v / population for k, v in sizes.items()}
    alloc = {k: max(1 if sizes[k] else 0, math.floor(r)) for k, r in raw.items()}
    alloc = {k: min(a, sizes[k]) for k, a in alloc.items()}
    remainders = sorted(raw, key=lambda k: (raw[k] - math.floor(raw[k]), sizes[k], k), reverse=True)
    while sum(alloc.values()) < total:
        progressed = False
        for k in remainders:
            if sum(alloc.values()) >= total:
                break
            if alloc[k] < sizes[k]:
                alloc[k] += 1
                progressed = True
        if not progressed:
            break
    while sum(alloc.values()) > total:
        k = max((k for k in alloc if alloc[k] > 1), key=lambda k: (alloc[k], k))
        alloc[k] -= 1
    return alloc


def systematic_pick(members: pd.DataFrame, n: int, seed: str, stratum: str) -> list[str]:
    if n <= 0 or members.empty:
        return []
    ordered = members.sort_values(["page_count", "native_chars", "filename"]).file_id.tolist()
    step = len(ordered) / n
    start = _seeded_unit(seed, stratum) * step
    return [ordered[min(len(ordered) - 1, int(start + i * step))] for i in range(n)]


def build_sample(frame: pd.DataFrame, size: int, seed: str) -> pd.DataFrame:
    frame = frame.copy()
    frame["stratum"] = frame.heuristic_doc_type + "|" + frame.text_profile.map(_text_group)
    selected: dict[str, str] = {}
    for fid, reason in [*edge_case_ids(frame), *family_showcase_ids(frame)]:
        if len(selected) < size:
            selected.setdefault(fid, reason)

    remaining = frame[~frame.file_id.isin(selected)]
    sizes = remaining.groupby("stratum").size().to_dict()
    alloc = allocate(sizes, size - len(selected))
    for stratum, n in sorted(alloc.items()):
        members = remaining[remaining.stratum == stratum]
        for fid in systematic_pick(members, n, seed, stratum):
            selected.setdefault(fid, "stratified")

    sample = frame[frame.file_id.isin(selected)].copy()
    sample["sample_reason"] = sample.file_id.map(selected)
    return sample.sort_values("filename")


def run_sampling() -> pd.DataFrame:
    settings = get_settings()
    settings.ensure_dirs()
    with session_scope() as s:
        rows = s.query(CorpusFile).all()
        if not rows:
            raise RuntimeError("inventory is empty; run `contracts inventory` first")
        frame = pd.DataFrame(
            [{c.name: getattr(r, c.name) for c in CorpusFile.__table__.columns} for r in rows]
        )
    sample = build_sample(frame, settings.sample_size, settings.sample_seed)

    with session_scope() as s:
        for cf in s.query(CorpusFile).all():
            row = sample[sample.file_id == cf.file_id]
            cf.in_sample = not row.empty
            cf.sample_stratum = row.stratum.iloc[0] if not row.empty else None
            cf.sample_reason = row.sample_reason.iloc[0] if not row.empty else None

    target = settings.sample_pdf_dir
    target.mkdir(parents=True, exist_ok=True)
    keep = set(sample.filename)
    for existing in target.glob("*.pdf"):
        if existing.name not in keep:
            existing.unlink()
    for rec in sample.itertuples():
        out = target / rec.filename
        if out.exists():
            continue
        raw = read_member(settings.zip_path, rec.zip_member)
        try:
            body, _ = strip_wrapper(raw)
        except Exception:
            body = raw
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(body)
        shutil.move(tmp, out)

    columns = [
        "file_id",
        "filename",
        "stratum",
        "sample_reason",
        "heuristic_doc_type",
        "text_profile",
        "page_count",
        "native_chars",
        "family_id",
        "duplicate_group",
    ]
    sample[columns].to_csv(settings.processed_dir / "sample_manifest.csv", index=False)
    return sample
