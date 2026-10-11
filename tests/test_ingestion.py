import pandas as pd
import pymupdf
import pytest

from berkshire_contracts.ingestion.chunking import chunk_page, heading_of
from berkshire_contracts.ingestion.filenames import contract_numbers, heuristic_doc_type, parse_filename
from berkshire_contracts.ingestion.pdf import PdfOpenError, open_pdf, strip_wrapper
from berkshire_contracts.ingestion.sampling import allocate, systematic_pick


def word_count(texts):
    return [len(t.split()) for t in texts]


def make_pdf(text: str = "This Agreement renews annually.", user_pw: str | None = None) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    if user_pw is None:
        return doc.tobytes()
    return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner-secret", user_pw=user_pw)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("23173_2_Applied_Technologies_Agreement.pdf", ["23173-2"]),
        ("16069_9_26_19_Agreement.pdf", ["16069"]),
        ("22128_1_Rate_Adjustment_and_21164.pdf", ["22128-1", "21164"]),
        ("Renewal_Letter_2025_20261.pdf", []),
        ("Quote_12345.pdf", []),
    ],
)
def test_contract_numbers(filename, expected):
    assert contract_numbers(filename) == expected


def test_parse_filename_family_and_resolution():
    facts = parse_filename("23173_2_Applied_Technologies_Agreement.pdf")
    assert (facts.family_id, facts.sub_contract_id) == ("23173", "23173-2")
    assert parse_filename("24_1234_Resolution.pdf").family_id == "24-1234"
    assert parse_filename("Board_Minutes.pdf").family_id is None


@pytest.mark.parametrize(
    ("filename", "doc_type"),
    [
        ("Vendor_Disclosure_Statement.pdf", "vendor_disclosure"),
        ("16069_Renewal_Letter.pdf", "renewal_extension"),
        ("21164_Modification_3.pdf", "amendment_modification"),
        ("16249_Motorola_Lease.pdf", "lease"),
        ("22128_Rate_Adjustment.pdf", "price_adjustment"),
        ("scan0001.pdf", "other"),
    ],
)
def test_heuristic_doc_type(filename, doc_type):
    assert heuristic_doc_type(filename) == doc_type


def test_allocate_is_proportional_with_floor_of_one():
    alloc = allocate({"agreement": 60, "renewal": 30, "lease": 1}, 10)
    assert sum(alloc.values()) == 10
    assert alloc["lease"] == 1
    assert alloc["agreement"] > alloc["renewal"]
    assert allocate({"a": 2, "b": 2}, 10) == {"a": 2, "b": 2}
    assert allocate({"a": 0}, 5) == {"a": 0}


def test_systematic_pick_is_deterministic():
    members = pd.DataFrame(
        {"file_id": [f"f{i}" for i in range(20)], "page_count": range(20), "native_chars": 0, "filename": "x"}
    )
    first = systematic_pick(members, 5, "seed", "stratum")
    assert first == systematic_pick(members, 5, "seed", "stratum")
    assert len(set(first)) == 5


def test_strip_wrapper_removes_upload_envelope():
    pdf = make_pdf()
    wrapped = (
        b"------WebKitFormBoundary\r\nContent-Type: application/pdf\r\n\r\n" + pdf + b"\r\n------WebKit--\r\n"
    )
    body, offset = strip_wrapper(wrapped)
    assert offset > 0 and body.startswith(b"%PDF") and body.rstrip().endswith(b"%%EOF")
    with pytest.raises(PdfOpenError):
        strip_wrapper(b"<html>not a pdf</html>")


def test_open_pdf_handles_wrapper_and_empty_password_encryption():
    opened = open_pdf(b"--boundary\r\n\r\n" + make_pdf())
    assert any("wrapper" in n for n in opened.notes)
    assert "renews annually" in opened.doc[0].get_text()

    encrypted = open_pdf(make_pdf(user_pw=""))
    assert encrypted.encrypted
    assert "renews annually" in encrypted.doc[0].get_text()


def test_open_pdf_rejects_password_protected_and_corrupt_files():
    with pytest.raises(PdfOpenError):
        open_pdf(make_pdf(user_pw="locked"))
    with pytest.raises(PdfOpenError):
        open_pdf(b"%PDF-1.7\n this is not really a pdf \n%%EOF")


def test_heading_detection():
    assert heading_of("SECTION 4. TERMINATION FOR CONVENIENCE\nThe County may terminate") is not None
    assert heading_of("12.3 Insurance Requirements") == "12.3 Insurance Requirements"
    assert heading_of("the county may terminate this agreement at any time.") is None


def test_chunks_respect_token_budget_and_page():
    long_paragraph = " ".join(["The agreement renews annually unless either party objects."] * 60)
    text = f"SECTION 1. TERM\n\n{long_paragraph}\n\nSECTION 2. PAYMENT\n\nInvoices are due net 30 days."
    chunks, section = chunk_page(
        "doc", 3, text, None, word_count, max_tokens=50, overlap_tokens=10, start_ordinal=0
    )
    assert len(chunks) > 5
    assert all(c.token_count <= 50 for c in chunks)
    assert all(c.page_no == 3 and c.chunk_id.startswith("doc:3:") for c in chunks)
    assert chunks[-1].section == "SECTION 2. PAYMENT"
    assert section == "SECTION 2. PAYMENT"
    assert "net 30 days" in chunks[-1].text


def test_oversized_sentence_is_split_by_words():
    run_on = " ".join(f"word{i}" for i in range(500))
    chunks, _ = chunk_page(
        "doc", 1, run_on, None, word_count, max_tokens=40, overlap_tokens=0, start_ordinal=0
    )
    assert all(c.token_count <= 40 for c in chunks)
    assert " ".join(c.text for c in chunks).split() == run_on.split()
