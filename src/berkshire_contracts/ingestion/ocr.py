"""Tesseract OCR (via the tesserocr wheel, which bundles libtesseract) with orientation recovery."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pymupdf
from PIL import Image

_STATE: dict[str, Any] = {}


@dataclass
class OcrResult:
    text: str
    confidence: float
    rotation: int


def _apis(tessdata: str) -> tuple[Any, Any]:
    if "api" not in _STATE:
        from tesserocr import OEM, PSM, PyTessBaseAPI

        _STATE["api"] = PyTessBaseAPI(path=tessdata, lang="eng", psm=PSM.AUTO, oem=OEM.LSTM_ONLY)
        _STATE["osd"] = PyTessBaseAPI(path=tessdata, lang="osd", psm=PSM.OSD_ONLY)
    return _STATE["api"], _STATE["osd"]


def _recognize(api: Any, image: Image.Image) -> tuple[str, float]:
    api.SetImage(image)
    text = api.GetUTF8Text() or ""
    return text, float(api.MeanTextConf())


def ocr_image(image: Image.Image, tessdata: str) -> OcrResult:
    api, osd = _apis(tessdata)
    text, conf = _recognize(api, image)
    if conf >= 65 and len(text.strip()) >= 40:
        return OcrResult(text, conf, 0)

    candidates: list[int] = []
    try:
        osd.SetImage(image)
        info = osd.DetectOrientationScript() or {}
        degrees = int(info.get("orient_deg") or 0)
        if degrees:
            candidates += [degrees, (360 - degrees) % 360]
    except Exception:
        pass
    for angle in (90, 180, 270):
        if angle not in candidates:
            candidates.append(angle)

    best = OcrResult(text, conf, 0)
    for angle in candidates[:3]:
        rotated_text, rotated_conf = _recognize(api, image.rotate(angle, expand=True))
        if rotated_conf > best.confidence + 5 and len(rotated_text.strip()) >= len(best.text.strip()) * 0.5:
            best = OcrResult(rotated_text, rotated_conf, angle)
    return best


def render_page(pdf_path: Path, page_no: int, dpi: int) -> Image.Image:
    from berkshire_contracts.ingestion.pdf import open_pdf

    cache: dict[str, pymupdf.Document] = _STATE.setdefault("docs", {})
    key = str(pdf_path)
    if key not in cache:
        if len(cache) >= 2:
            for old in list(cache)[:-1]:
                cache.pop(old).close()
        cache[key] = open_pdf(pdf_path.read_bytes()).doc
    page = cache[key][page_no - 1]
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def ocr_pdf_page(task: tuple[str, int, int, str]) -> tuple[str, int, OcrResult | None, str | None]:
    """Process-pool entry point: returns (path, page_no, result, error)."""
    pdf_path, page_no, dpi, tessdata = task
    try:
        image = render_page(Path(pdf_path), page_no, dpi)
        return pdf_path, page_no, ocr_image(image, tessdata), None
    except Exception as exc:
        return pdf_path, page_no, None, f"{type(exc).__name__}: {exc}"
