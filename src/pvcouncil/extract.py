"""PDF -> per-page text extraction using PyMuPDF, with OCR fallback."""

import os
from pathlib import Path

import fitz

MIN_TEXT_CHARS = 50  # below this, a page with an image is treated as scanned


def _tessdata_dir() -> str | None:
    """Locate Tesseract language data for PyMuPDF's OCR support."""
    candidates = (
        os.environ.get("TESSDATA_PREFIX"),
        "/opt/homebrew/share/tessdata",
        "/usr/local/share/tessdata",
        "/usr/share/tesseract-ocr/5/tessdata",
        "/usr/share/tessdata",
    )
    for p in candidates:
        if p and Path(p).is_dir():
            return p
    return None


def _ocr_page_text(page: fitz.Page, tessdata: str) -> str:
    """OCR a page that has no embedded text layer."""
    try:
        textpage = page.get_textpage_ocr(full=True, dpi=300, tessdata=tessdata)
        return page.get_text("text", textpage=textpage).strip()
    except Exception:
        return ""


def extract_pages(pdf_path: Path, ocr: bool = True) -> list[tuple[int, str]]:
    """Extract text from each page of a PDF.

    Returns list of (1-based page_number, text) tuples.
    Scanned pages (no embedded text, or only a few characters over an
    image) are OCR'd via Tesseract when available; pages that still yield
    nothing are skipped.
    """
    path = Path(pdf_path)
    if path.suffix.lower() == ".docx":
        return []  # skip docx files

    tessdata = _tessdata_dir() if ocr else None

    doc = fitz.open(str(path))
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        # A scanned page can still carry a stray text layer, such as a typed
        # page number over an image of a table, so near-empty pages with an
        # image are OCR'd too.
        if tessdata and len(text) < MIN_TEXT_CHARS and (not text or page.get_images()):
            text = _ocr_page_text(page, tessdata) or text
        if text:
            pages.append((i + 1, text))
    doc.close()
    return pages


def has_ocr_gap(pdf_path: Path) -> bool:
    """True if a page has a stray text layer over an image.

    Indexes built before MIN_TEXT_CHARS existed skipped OCR on such pages, so
    their scanned content is missing from the index.
    """
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            text = page.get_text("text").strip()
            if 0 < len(text) < MIN_TEXT_CHARS and page.get_images():
                return True
    return False


def extract_text(pdf_path: Path) -> str:
    """Extract all text from a PDF as a single string."""
    pages = extract_pages(pdf_path)
    return "\n\n".join(text for _, text in pages)
