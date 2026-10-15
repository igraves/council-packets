"""Read the restriction instruments themselves: download, OCR, and classify them.

Starts with the instruments filed with a plat (deeds/restrictions.json,
"filed_with_plat"). Each image is OCR'd upright, page by page. The text says
what the instrument is: a declaration of restrictions, a homes association
declaration, an easement, a deed. It also gives the terms that matter: how long
the restrictions run, setbacks, use limits, and whether the instrument carries a
racially restrictive covenant. Those were common in Kansas City-area
restrictions of the 1920s-50s, are unenforceable (Shelley v. Kraemer, 1948), and
Kansas lets owners strike them.

Images and OCR text stay local (gitignored: they name signers and owners).
deeds/restriction_reads.json carries only the classification and terms.
"""

import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import fitz

from pvcouncil import ocrengines, plats, transcripts
from pvcouncil.recorder import Recorder

IMAGES_DIR = plats.IMAGES_DIR / "restrictions"
OCR_DIR = plats.DEEDS_DIR / "ocr" / "restrictions"
RESULTS_PATH = plats.DEEDS_DIR / "restriction_reads.json"
EXCERPTS_PATH = OCR_DIR / "excerpts.md"  # local only
DPI = 250


def image_path(e: dict) -> Path:
    return IMAGES_DIR / f"{plats._slug(e['book_page'])}--{e['id']}.pdf"


def download(entries: list[dict], echo=print) -> dict:
    """Download each instrument's image; skips ones already downloaded."""
    todo = [e for e in entries if not image_path(e).exists()]
    echo(f"{len(entries)} instruments, fetching {len(todo)} images")
    stats = {"downloaded": 0, "no_image": 0, "bytes": 0}
    if not todo:
        return stats
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    rec = Recorder().login()
    for i, e in enumerate(todo, 1):
        if not rec.image_detail(e["id"]).get("HasImage"):
            stats["no_image"] += 1
            echo(f"[{i}/{len(todo)}] {e['book_page']}: no image")
            continue
        data = rec.image_pdf(e["id"])
        image_path(e).write_bytes(data)
        stats["downloaded"] += 1
        stats["bytes"] += len(data)
        if i % 10 == 0 or i == len(todo):
            echo(f"[{i}/{len(todo)}] {stats['bytes'] / 1e6:.0f} MB")
    return stats


def _ocr_page(page, rot: int, invert: bool = False) -> str:
    pix = page.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72).prerotate(rot), colorspace=fitz.csGRAY)
    if invert:
        pix.invert_irect()
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        pix.save(f.name)
        return subprocess.run(["tesseract", f.name, "-"], capture_output=True, text=True,
                              env={**os.environ, "OMP_THREAD_LIMIT": "1"}).stdout.strip()


def _real_words(text: str) -> int:
    return len(re.findall(r"\b[a-z]{3,}\b", text.lower()))


def ocr_doc(pdf_path: str) -> tuple[str, str]:
    """OCR a typed instrument page by page. Many 1950s instruments are photostat negatives
    (white type on black) and some pages were scanned sideways, so a page that reads poorly
    is retried inverted, then at 90 and 270 degrees, and the best reading kept."""
    pages = []
    for i, page in enumerate(fitz.open(pdf_path), 1):
        text = _ocr_page(page, 0)
        if _real_words(text) < 60:
            text = max([text, _ocr_page(page, 0, invert=True)], key=_real_words)
        if _real_words(text) < 60:
            text = max([text] + [_ocr_page(page, r, inv) for r in (90, 270) for inv in (False, True)],
                       key=_real_words)
        pages.append(f"[page {i}]\n{text}")
    return pdf_path, "\n\n".join(pages)


def ocr_all(entries: list[dict], workers: int = 6, echo=print) -> int:
    OCR_DIR.mkdir(parents=True, exist_ok=True)
    todo = [str(image_path(e)) for e in entries
            if image_path(e).exists() and not (OCR_DIR / f"{image_path(e).stem}.txt").exists()]
    echo(f"{len(todo)} images to OCR")
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(ocr_doc, t) for t in todo]):
            path, text = fut.result()
            (OCR_DIR / f"{Path(path).stem}.txt").write_text(text)
            done += 1
            if done % 10 == 0 or done == len(todo):
                echo(f"  OCR {done}/{len(todo)}")
    return done


# what the instrument is, checked in order; the first that matches wins
KINDS = [
    ("homes association", r"homes?\s+association|home\s*owners'?\s+association"),
    ("restrictions", r"declaration\s+of\s+restrictions|restrictions?\s+(?:and|&)\s+covenants|"
                     r"hereby\s+restricted|(?:covenants|restrictions),?\s+(?:restrictions|covenants)\s+and\s+stipulations"),
    ("restriction amendment", r"(?:amend|modif|extend|renew)\w*\s+(?:of\s+)?(?:the\s+)?(?:said\s+)?restrictions"),
    ("release", r"\brelease\s+of\b|hereby\s+releases?"),
    ("easement", r"\beasement\b"),
    ("dedication", r"\bdedicat"),
    ("deed", r"warranty\s+deed|quit\s*claim|grant,?\s+bargain\s+and\s+sell"),
    ("affidavit", r"\baffidavit\b"),
]

FEATURES = {
    "residence only": r"private\s+residence|residence\s+purposes\s+only|single[\s-]+family",
    "no apartments": r"flat\s+or\s+apartment|apartment\s+house",
    "building line / setback": r"building\s+line|set\s*back|feet\s+(?:back\s+)?from\s+the\s+(?:front|street)",
    "minimum cost": r"(?:cost|value)\s+(?:of\s+)?(?:not\s+)?less\s+than|minimum\s+cost",
    "minimum floor area": r"(?:floor|ground)\s+area|square\s+feet",
    "architectural approval": r"plans?\s+(?:and\s+specifications\s+)?(?:shall\s+(?:first\s+)?be\s+)?(?:submitted|approved)",
    "homes association assessments": r"assessments?|annual\s+(?:charge|dues)",
    "renewal": r"renew(?:ed|able|al)|automatically\s+(?:extended|continue)",
    "racial restriction": r"\bnegro|african\s+descent|colou?red\s+(?:race|person)|"
                          r"persons?\s+(?:of|having)\s+any\s+(?:negro|african)|"
                          r"(?:race|racial)\s+other\s+than|\bcaucasian|white\s+race",
}

_YEARS = re.compile(r"period\s+of\s+([a-z-]+(?:\s+[a-z-]+)?|\d+)\s*(?:\(\d+\)\s*)?years", re.I)
_UNTIL = re.compile(r"until\s+(?:the\s+)?(?:\w+\s+){0,3}(19\d\d|20\d\d)", re.I)


def classify(text: str) -> dict:
    flat = re.sub(r"\s+", " ", text)
    kind = next((k for k, pat in KINDS if re.search(pat, flat, re.I)), "other")
    feats = [f for f, pat in FEATURES.items() if re.search(pat, flat, re.I)]
    # a garbled heading can hide a declaration; its terms still give it away
    use_terms = set(feats) - {"homes association assessments", "minimum floor area", "renewal"}
    if kind in ("other", "easement", "dedication", "deed") and len(use_terms) >= 2:
        kind = "restrictions"
    term = _YEARS.search(flat)
    until = _UNTIL.search(flat)
    return {
        "kind": kind,
        "features": feats,
        "term": term.group(0) if term else None,
        "until_year": int(until.group(1)) if until else None,
        "pages": flat.count("[page "),
        "words": len(re.findall(r"[A-Za-z]{3,}", flat)),
    }


def _excerpt(text: str, pat: str, width: int = 300) -> list[str]:
    flat = re.sub(r"\s+", " ", text)
    return [flat[max(0, m.start() - width // 2): m.end() + width // 2] for m in re.finditer(pat, flat, re.I)][:3]


REVIEW_PATH = plats.DEEDS_DIR / "restriction_review.json"


def build(entries: list[dict]) -> dict:
    review = json.loads(REVIEW_PATH.read_text()) if REVIEW_PATH.exists() else {}
    manual, chosen = transcripts.load(), ocrengines.adopted()
    rows, excerpts = [], ["# Restriction instrument excerpts (local only)", ""]
    for e in entries:
        txt = OCR_DIR / f"{image_path(e).stem}.txt"
        if not txt.exists():
            continue
        tess = txt.read_text()
        # a reading from another engine, adopted in the viewer, stands in for Tesseract's
        text, eng = ocrengines.base_text(f"r:{e['id']}", tess, image_path(e), chosen)
        base = classify(text)
        typed = transcripts.text_for(f"r:{e['id']}", manual)
        if typed:
            text += "\n\n" + typed
        c = classify(text)
        c["words"] = classify(tess)["words"]  # what Tesseract alone read
        if eng:
            c.update(engine=eng["label"], engine_stale=eng["stale"],
                     engine_terms=sorted(set(base["features"]) - set(classify(tess)["features"])))
        items = manual.get(f"r:{e['id']}", [])
        c["transcribed"] = len(items)
        if items:
            # terms only a transcription found, cited to its page and region
            c["transcribed_terms"] = transcripts.attribute(items, set(base["features"]),
                                                           lambda t: classify(t)["features"])
            fp = transcripts.fingerprint(image_path(e))
            c["stale_transcriptions"] = sum(transcripts.is_stale(x, fp) is True for x in items)
        r = review.get(e["book_page"])
        if r:
            c.update(kind=r["kind"], note=r["note"], unrelated=r.get("unrelated", False), reviewed=True)
        rows.append({"id": e["id"], "book_page": e["book_page"], "recorded": e["recorded"],
                     "docid": e["docid"], "plats": e["plats"], "plat_book_pages": e["plat_book_pages"], **c})
        if "racial restriction" in c["features"]:
            excerpts += [f"## {e['book_page']} ({e['recorded']}) {', '.join(e['plats'])}", ""]
            excerpts += [f"> {x}" for x in _excerpt(text, FEATURES["racial restriction"])] + [""]
    out = {"read": rows}
    RESULTS_PATH.write_text(json.dumps(out, indent=1) + "\n")
    EXCERPTS_PATH.write_text("\n".join(excerpts) + "\n")
    return out
