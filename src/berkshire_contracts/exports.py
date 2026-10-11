"""Reviewer-facing exports: one row per contract file (CSV/XLSX), field evidence, and a data dictionary."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlalchemy import select

from berkshire_contracts.chat.filters import vendor_groups
from berkshire_contracts.config import get_settings
from berkshire_contracts.db import Contract, FieldEvidence, get_engine
from berkshire_contracts.extraction.fields import FIELD_SPECS
from berkshire_contracts.schemas import EXTRACTION_FIELDS

CONTRACT_COLUMNS = [
    "file_id",
    "filename",
    "family_id",
    "duplicate_of",
    *EXTRACTION_FIELDS,
    "fields_found",
    "needs_review",
    "review_reasons",
    "extraction_status",
    "parse_status",
    "text_source",
    "page_count",
    "ocr_pages",
    "mean_ocr_confidence",
    "parse_notes",
    "extraction_model",
    "prompt_version",
    "extracted_at",
]

PROCESS_COLUMNS = {
    "file_id": "Stable id: first 12 hex chars of SHA-1 of the archive member path.",
    "filename": "Original PDF file name.",
    "family_id": "Contract family (5-digit number) linking agreements, amendments and letters.",
    "duplicate_of": "file_id of the byte-identical file whose content was indexed (null if unique).",
    "vendor_group": "Vendor name with spelling variants merged (derived; vendor_name is the value as written).",
    "fields_found": "Number of the 19 fields with a value.",
    "needs_review": "True when any value is low-confidence, unverified, inconsistent, or the parse was partial.",
    "review_reasons": "Why the row needs review.",
    "extraction_status": "ok, escalated, duplicate, failed, skipped_no_text, skipped_unreadable.",
    "parse_status": "ok, partial (some pages empty / low OCR confidence), no_text, failed.",
    "text_source": "native (PDF text layer), ocr (Tesseract), mixed, none.",
    "page_count": "Pages in the PDF.",
    "ocr_pages": "Pages whose text came from OCR.",
    "mean_ocr_confidence": "Mean Tesseract word confidence (0-100) over OCR pages.",
    "parse_notes": "Repairs, decryption, wrapper stripping, empty pages.",
    "extraction_model": "OpenAI model that produced the final values.",
    "prompt_version": "Extraction prompt version.",
    "extracted_at": "UTC timestamp of extraction.",
}


def contracts_frame() -> pd.DataFrame:
    with get_engine().connect() as conn:
        df = pd.read_sql(select(Contract.__table__), conn)
    df["vendor_group"] = vendor_groups(df["vendor_name"])
    columns = [c for c in CONTRACT_COLUMNS if c in df.columns]
    columns.insert(columns.index("vendor_name") + 1, "vendor_group")
    return df[columns].sort_values("filename").reset_index(drop=True)


def evidence_frame() -> pd.DataFrame:
    with get_engine().connect() as conn:
        ev = pd.read_sql(select(FieldEvidence.__table__), conn)
        names = pd.read_sql(select(Contract.file_id, Contract.filename), conn)
    ev = ev.merge(names, on="file_id", how="left")
    cols = [
        "file_id",
        "filename",
        "field_name",
        "status",
        "value_text",
        "page",
        "quote",
        "confidence",
        "verified",
        "match_score",
        "escalated",
        "model",
        "prompt_version",
    ]
    return ev[cols].sort_values(["filename", "field_name"]).reset_index(drop=True)


def data_dictionary() -> pd.DataFrame:
    rows = [
        {
            "column": s.name,
            "label": s.label,
            "group": s.group,
            "type": s.kind,
            "definition": s.instruction,
            "business_use": s.business_use,
        }
        for s in FIELD_SPECS
    ]
    rows += [
        {"column": k, "label": k, "group": "Process", "type": "", "definition": v, "business_use": ""}
        for k, v in PROCESS_COLUMNS.items()
    ]
    return pd.DataFrame(rows)


def write_exports(out_dir: Path | None = None) -> dict[str, str]:
    out_dir = out_dir or get_settings().exports_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    contracts, evidence, dictionary = contracts_frame(), evidence_frame(), data_dictionary()
    paths = {
        "contracts_csv": out_dir / "contracts.csv",
        "evidence_csv": out_dir / "field_evidence.csv",
        "xlsx": out_dir / "contracts.xlsx",
    }
    contracts.to_csv(paths["contracts_csv"], index=False)
    evidence.to_csv(paths["evidence_csv"], index=False)
    with pd.ExcelWriter(paths["xlsx"], engine="xlsxwriter") as xl:
        contracts.to_excel(xl, sheet_name="contracts", index=False)
        evidence.to_excel(xl, sheet_name="field_evidence", index=False)
        dictionary.to_excel(xl, sheet_name="data_dictionary", index=False)
        for name, frame in (
            ("contracts", contracts),
            ("field_evidence", evidence),
            ("data_dictionary", dictionary),
        ):
            sheet = xl.sheets[name]
            sheet.freeze_panes(1, 2 if name != "data_dictionary" else 1)
            sheet.autofilter(0, 0, len(frame), len(frame.columns) - 1)
            for i, col in enumerate(frame.columns):
                width = min(
                    60, max(10, int(frame[col].astype(str).str.len().quantile(0.9) if len(frame) else 10))
                )
                sheet.set_column(i, i, width)
    return {k: str(v) for k, v in paths.items()} | {"rows": str(len(contracts))}
