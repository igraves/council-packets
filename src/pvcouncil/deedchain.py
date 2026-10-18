"""Follow sample lots' deeds from the developer's first sale to today, looking for racial language
written into individual deeds rather than (or as well as) a recorded declaration.

Each lot's recorded history comes from the recorder's legal search (subdivision, block, lot). The
county indexed Prairie Village's original 1940s deeds by lot even where their short legal is blank.
Every deed in a history is downloaded and OCR'd, then read for a racial clause and for how it treats
the subdivision's restrictions: restated in the deed, or only cited ("subject to restrictions of record").

Searches (which name the parties), images and OCR text stay local (gitignored). deeds/deed_chain.json
carries only the lot, date, book and page, page count and what the deed says about restrictions.
"""

import json
import re
from concurrent.futures import ProcessPoolExecutor, as_completed

import fitz

from pvcouncil import plats, restrictread
from pvcouncil.recorder import Recorder

CACHE_DIR = plats.DEEDS_DIR / "cache" / "chain"
IMAGES_DIR = plats.IMAGES_DIR / "chain"
OCR_DIR = plats.DEEDS_DIR / "ocr" / "chain"
RESULTS_PATH = plats.DEEDS_DIR / "deed_chain.json"

# Prairie Village lots at the four corners of Roe Avenue and 71st Street, and along 71st just east,
# where the stories place deeds with racial language; houses built 1946-1953
SAMPLE = [
    ("PRAIRIE VILLAGE", "17", "1"), ("PRAIRIE VILLAGE", "17", "7"), ("PRAIRIE VILLAGE", "17", "11"),
    ("PRAIRIE VILLAGE", "19", "23"), ("PRAIRIE VILLAGE", "19", "24"), ("PRAIRIE VILLAGE", "19", "25"),
    ("PRAIRIE VILLAGE", "19", "37"),
    ("PRAIRIE VILLAGE", "39", "2"), ("PRAIRIE VILLAGE", "39", "3"), ("PRAIRIE VILLAGE", "39", "16"),
    ("PRAIRIE VILLAGE", "51", "2"), ("PRAIRIE VILLAGE", "51", "3"),
    ("PRAIRIE VILLAGE", "14", "29"), ("PRAIRIE VILLAGE", "14", "30"),
    ("PRAIRIE VILLAGE", "18", "4"), ("PRAIRIE VILLAGE", "18", "5"),
    ("PRAIRIE VILLAGE", "15", "1"), ("PRAIRIE VILLAGE", "15", "24"),
    # where Tomahawk Road meets 72nd Terrace: Block 20 north of 72nd Terrace, Block 21 between it and
    # Tomahawk, Block 43 across Roe, and all of Block 36 (7401 Roe, its house lots since combined),
    # under the 1950 race-only declaration; an empty lot follows the whole block
    ("PRAIRIE VILLAGE", "20", "1"), ("PRAIRIE VILLAGE", "20", "3"), ("PRAIRIE VILLAGE", "20", "26"),
    ("PRAIRIE VILLAGE", "20", "30"), ("PRAIRIE VILLAGE", "20", "35"), ("PRAIRIE VILLAGE", "20", "37"),
    ("PRAIRIE VILLAGE", "20", "38"),
    ("PRAIRIE VILLAGE", "21", "1"), ("PRAIRIE VILLAGE", "21", "2"), ("PRAIRIE VILLAGE", "21", "4"),
    ("PRAIRIE VILLAGE", "21", "15"), ("PRAIRIE VILLAGE", "21", "33"), ("PRAIRIE VILLAGE", "21", "39"),
    ("PRAIRIE VILLAGE", "21", "40"),
    ("PRAIRIE VILLAGE", "43", "85"),
    ("PRAIRIE VILLAGE", "36", ""),
]

RACIAL = re.compile(restrictread.FEATURES["racial restriction"] + r"|person\s+of\s+color|\bmongolian|"
                    r"asiatic|ethiopian|malay|\bmulatto|semitic|\bjews?\b|\bhebrew", re.I)
CITES = re.compile(r"(?:subject\s+to|except)[^.;]{0,80}?(?:restrictions|covenants)|"
                   r"restrictions?[^.;]{0,40}of\s+record", re.I)
RESTATES = re.compile(restrictread.FEATURES["building line / setback"] + "|" +
                      restrictread.FEATURES["minimum cost"], re.I)


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")


def lot_path(sub: str, block: str, lot: str):
    return CACHE_DIR / f"{_slug(sub)}-{block}-{lot or 'all'}.json"


def image_path(d: dict):
    return IMAGES_DIR / f"{plats._slug(d['book_page'])}--{d['id']}.pdf"


def ocr_path(d: dict):
    return OCR_DIR / f"{image_path(d).stem}.txt"


def fetch(lots=SAMPLE, echo=print) -> None:
    """Each lot's full recorded history; skips lots already cached."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    todo = [x for x in lots if not lot_path(*x).exists()]
    if not todo:
        return
    rec = Recorder().login()
    for i, (sub, block, lot) in enumerate(todo, 1):
        legal = [("subdivision", "IS", sub), ("block", "IS", block)] + ([("lot", "IS", lot)] if lot else [])
        rows = rec.search(legal=legal)
        lot_path(sub, block, lot).write_text(json.dumps(rows))
        echo(f"[{i}/{len(todo)}] {sub} Block {block} Lot {lot or 'all'}: "
             f"{len({r['instrumentReference_id'] for r in rows})} instruments")


def deeds(lots=SAMPLE) -> list[dict]:
    """Every deed in the cached histories, oldest first, one entry per instrument and lot."""
    out = []
    for sub, block, lot in lots:
        p = lot_path(sub, block, lot)
        if not p.exists():
            continue
        seen = set()
        for r in sorted(json.loads(p.read_text()), key=lambda r: r["RecordedDate"]):
            if r["instrumentReference_id"] in seen or "deed" not in (r["Value"] or "").lower():
                continue
            seen.add(r["instrumentReference_id"])
            out.append({"id": r["instrumentReference_id"], "book_page": r["ReferenceDisplayValue"].strip(),
                        "recorded": r["RecordedDate"][:10], "type": r["Value"].strip(),
                        "subdivision": sub, "block": block, "lot": lot})
    return out


def download(entries: list[dict], echo=print) -> None:
    todo = list({d["id"]: d for d in entries if not image_path(d).exists()}.values())
    echo(f"{len(entries)} deeds, fetching {len(todo)} images")
    if not todo:
        return
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    rec = Recorder().login()
    for i, d in enumerate(todo, 1):
        if not rec.image_detail(d["id"]).get("HasImage"):
            echo(f"[{i}/{len(todo)}] {d['book_page']}: no image")
            continue
        image_path(d).write_bytes(rec.image_pdf(d["id"]))
        if i % 10 == 0 or i == len(todo):
            echo(f"[{i}/{len(todo)}] downloaded")


def ocr(entries: list[dict], workers: int = 6, echo=print) -> None:
    todo = list({d["id"]: d for d in entries if image_path(d).exists() and not ocr_path(d).exists()}.values())
    if not todo:
        return
    OCR_DIR.mkdir(parents=True, exist_ok=True)
    by_path = {str(image_path(d)): d for d in todo}
    with ProcessPoolExecutor(workers) as pool:
        futs = [pool.submit(restrictread.ocr_doc, p) for p in by_path]
        for n, fut in enumerate(as_completed(futs), 1):
            path, text = fut.result()
            ocr_path(by_path[path]).write_text(text)
            if n % 10 == 0 or n == len(todo):
                echo(f"  OCR {n}/{len(todo)}")


def text_for(d: dict) -> tuple[str, str]:
    """olmOCR's reading when there is one (it reads these far better), else Tesseract's."""
    from pvcouncil import ocrengines

    olm = ocrengines.load_reading("olmocr", f"d:{d['id']}")
    if olm:
        return ocrengines.reading_text(olm), "olmOCR-2"
    p = ocr_path(d)
    return (p.read_text(), "Tesseract") if p.exists() else ("", "")


def build(lots=SAMPLE) -> dict:
    rows, excerpts = [], ["# Deed chain racial excerpts (local only)", ""]
    for d in deeds(lots):
        text, engine = text_for(d)
        flat = re.sub(r"\s+", " ", text)
        pages = 0
        if image_path(d).exists():
            with fitz.open(image_path(d)) as pdf:
                pages = pdf.page_count
        m = RACIAL.search(flat)
        rows.append({**d, "pages": pages, "read_by": engine or None,
                     "words": len(re.findall(r"[A-Za-z]{3,}", flat)),
                     "racial": bool(m), "cites_restrictions": bool(CITES.search(flat)),
                     "restates_restrictions": bool(RESTATES.search(flat))})
        if m:
            excerpts += [f"## {d['subdivision']} Block {d['block']} Lot {d['lot'] or 'all'}: {d['book_page']} ({d['recorded']})",
                         "", f"> …{flat[max(0, m.start() - 300):m.end() + 300]}…", ""]
    out = {"lots": [{"subdivision": s, "block": b, "lot": lt} for s, b, lt in lots], "deeds": rows}
    RESULTS_PATH.write_text(json.dumps(out, indent=1) + "\n")
    OCR_DIR.mkdir(parents=True, exist_ok=True)
    (OCR_DIR / "excerpts.md").write_text("\n".join(excerpts) + "\n")
    return out
