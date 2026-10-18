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


CANDIDATE_MIN_SCORE = 5


def candidate_entries(out: dict, skip: set[str], n_new: int) -> list[dict]:
    """Walk candidates (restrictions.walk_candidates) tied to one of our plats: the ones already
    OCR'd, plus the next n_new by score (then date). They carry source "candidate"."""
    cands = sorted((c for c in out.get("misc_candidates", [])
                    if c["score"] >= CANDIDATE_MIN_SCORE and c["plats"] and c["id"] not in skip),
                   key=lambda c: (-c["score"], c["recorded"]))
    picked, seen = [], set()
    for c in cands:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        e = {"id": c["id"], "book_page": c["book_page"], "recorded": c["recorded"][:10], "docid": None,
             "plats": c["plats"], "plat_book_pages": [], "source": "candidate"}
        if (OCR_DIR / f"{image_path(e).stem}.txt").exists():
            picked.append(e)
        elif n_new > 0:
            picked.append(e)
            n_new -= 1
    return picked


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


# what the instrument is: the kind named first in its heading, else the first in this order that matches
KINDS = [
    ("homes association", r"homes?\s+association|home\s*owners'?\s+association"),
    ("restrictions", r"declaration\s+of\s+restrictions|restrictions?\s+(?:and|&)\s+covenants|"
                     r"hereby\s+restricted|(?:covenants|restrictions),?\s+(?:restrictions|covenants)\s+and\s+stipulations"),
    ("restriction amendment", r"(?:amend|modif|extend|renew)\w*\s+(?:of\s+|to\s+)?(?:the\s+)?(?:said\s+)?restrictions|"
                              r"modification\s+agreement"),
    ("release", r"\brelease\s+of\b|hereby\s+releases?|easement\s+release"),
    ("easement", r"\beasements?\b"),
    ("dedication", r"(?<!heretofore )\bdedicat"),  # not the recital "has heretofore dedicated and platted"
    ("deed", r"warranty\s+deed|quit\s*claim|grant,?\s+bargain\s+and\s+sell"),
    ("affidavit", r"\baffidavit\b|being\s+(?:first\s+)?duly\s+sworn"),
]
HEADING_CHARS = 200  # "MODIFICATION OF RESTRICTIONS ..." names the instrument before its body cites the declaration

FEATURES = {
    # "for other than residence purposes", "private dwelling purposes only"; not a bare "for
    # residential purposes", which also reads "shall not be used for residential purposes"
    "residence only": r"private\s+residence|single[\s-]+family|"
                      r"other\s+than\s+(?:private\s+)?(?:residen(?:ce|tial)|dwelling)\s+purposes|"
                      r"(?:residen(?:ce|tial)|dwelling)\s+purposes\s+only",
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
    head = [(m.start(), k) for k, pat in KINDS if (m := re.search(pat, flat[:HEADING_CHARS], re.I))]
    kind = min(head)[1] if head else next((k for k, pat in KINDS if re.search(pat, flat, re.I)), "other")
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


_NUM_WORDS = ("one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|"
              "seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred")
_WORDED = re.compile(rf"\b(?:(?:{_NUM_WORDS})[\s-]*)+\(\s*([\dlIO]{{1,3}})\s*\)", re.I)  # "Thirty-six (36)"
_NUM = r"[\dlIOh|]{1,3}"
_ITEM = rf"{_NUM}(?:-?[A-Z]\b)?"  # lettered blocks (1A, 2-A) are read past, not counted
_LIST = rf"{_ITEM}(?:(?:\s*(?:,|&|and|to|through|thru|-|–)\s*)+(?:both\s+inclusive,?\s*)?{_ITEM})*"
_LOTS_IN_BLOCK = re.compile(rf"\bLots?\s+({_LIST})[^.;]{{0,40}}?\bBlock\s+({_NUM})\b", re.I)
_BLOCKS = re.compile(rf"\bBlocks?\s+({_LIST})", re.I)


def _n(tok: str) -> int | None:
    tok = tok.translate(str.maketrans("lIOh|", "11041"))
    return int(tok) if tok.isdigit() else None


def _numbers(s: str) -> set[int]:
    out, prev, rng = set(), None, False
    for tok in re.findall(rf"{_NUM}(?:-?[A-Z]\b)?|to|through|thru|-|–", s, re.I):
        if tok.lower() in ("to", "through", "thru", "-", "–"):
            rng = prev is not None
            continue
        n = _n(tok)
        if n is None:
            continue
        if rng and prev is not None and 0 < n - prev < 200:
            out |= set(range(prev, n + 1))
        out.add(n)
        prev, rng = n, False
    return out


LAND_CHARS = 2500  # the land a declaration covers is described before its restrictions begin


def lots_blocks(text: str) -> dict[str, list[int] | None]:
    """Blocks a declaration covers, each with the lots named in it (None: the whole block, or lots not
    given). Keys are block numbers as strings, for JSON. OCR slips in numbers (l, I, O, h, |) are mended.

    Only the opening is read: the restrictions themselves name lots and blocks too (frontages, floor
    areas), and a declaration with no blocks in its opening covers the whole subdivision."""
    flat = re.sub(r"\s+", " ", text.replace("_", " "))[:LAND_CHARS]
    flat = _WORDED.sub(lambda m: m.group(1), flat)
    out: dict[int, set[int] | None] = {}
    for m in _LOTS_IN_BLOCK.finditer(flat):
        b = _n(m.group(2))
        if b:
            lots = _numbers(m.group(1))
            if out.get(b, set()) is not None:
                out[b] = (out.get(b) or set()) | lots
    for m in _BLOCKS.finditer(flat):
        for b in _numbers(m.group(1)):
            if b and b not in out:
                out[b] = None
    return {str(b): sorted(v) if v is not None else None for b, v in sorted(out.items()) if 0 < b < 200}


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
        # the blocks and lots it names, from olmOCR's reading when there is one (it reads lot and block
        # numbers far better than Tesseract), else the report text
        olm = ocrengines.load_reading("olmocr", f"r:{e['id']}")
        c["blocks"] = lots_blocks(ocrengines.reading_text(olm) if olm else text)
        if olm and not eng:
            # olmOCR reads the heading Tesseract garbles, so it names the kind; its terms join the rest
            o = classify(ocrengines.reading_text(olm) + ("\n\n" + typed if typed else ""))
            c.update(kind=o["kind"], kind_engine=olm["label"], term=c["term"] or o["term"],
                     until_year=c["until_year"] or o["until_year"],
                     unadopted_terms=[f for f in o["features"] if f not in c["features"]],
                     features=[f for f in FEATURES if f in c["features"] or f in o["features"]])
        r = review.get(e["book_page"])
        if r and "kind" in r:
            c.update(kind=r["kind"], note=r["note"], unrelated=r.get("unrelated", False), reviewed=True)
        if r and "blocks" in r:  # read by eye: {"1": "1-34", "6": null, ...}
            c["blocks"] = {b: sorted(_numbers(v)) if v else None for b, v in r["blocks"].items()}
        rows.append({"id": e["id"], "book_page": e["book_page"], "recorded": e["recorded"],
                     "docid": e["docid"], "plats": e["plats"], "plat_book_pages": e["plat_book_pages"],
                     **({"source": e["source"]} if e.get("source") else {}), **c})
        if "racial restriction" in c["features"]:
            excerpts += [f"## {e['book_page']} ({e['recorded']}) {', '.join(e['plats'])}", ""]
            excerpts += [f"> {x}" for x in _excerpt(text, FEATURES["racial restriction"])] + [""]
    out = {"read": rows}
    RESULTS_PATH.write_text(json.dumps(out, indent=1) + "\n")
    EXCERPTS_PATH.write_text("\n".join(excerpts) + "\n")
    return out
