"""OCR the plat images and find the restrictions they state or cite.

Plats are large single-sheet scans with no text layer, and some print their
dedication sideways, so each image is OCR'd at 0, 90, and 270 degrees and
only lines that read as words are kept. Full text is cached in deeds/ocr/
(gitignored). From the text this finds:

  - restriction language written on the plat itself (use, setbacks, minimum
    cost, homes association), and
  - citations to separately recorded restrictions, usually "Book 22 of
    Miscellaneous Records, page 306" or "Misc. Book 22, page 306",

and writes deeds/plat_restrictions.json for the restriction search.
"""

import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import fitz

from pvcouncil import plats

OCR_DIR = plats.DEEDS_DIR / "ocr"
RESULTS_PATH = plats.DEEDS_DIR / "plat_restrictions.json"
DPI = 250
ROTATIONS = (0, 90, 270)

# phrases that mark restriction language on a plat
RESTRICTION_TERMS = {
    "restrictions": r"\brestrict\w*",
    "covenants": r"\bcovenant\w*",
    "homes association": r"\bhomes?\s+associat\w*",
    "residential use": r"\b(?:private|single[- ]family|detached)\s+(?:residen\w*|dwelling\w*)|\bresidence purposes\b",
    "minimum cost": r"\bminimum\s+cost\b|\bcost\s+of\s+(?:any\s+)?dwelling",
    "building line": r"\bbuilding\s+(?:set\s*back\s+)?line\b|\bset\s*back\b",
    "outbuildings": r"\boutbuilding\w*",
    "declaration": r"\bdeclaration\b",
}
# "Book 22 of Miscellaneous Records, page 306" / "Misc. Book 22, page 306" / "Book 22 Misc., Page 306"
_NUM = r"(\d{1,3})"
CITATION_PATTERNS = [
    rf"Book\s*{_NUM}\s*(?:of\s+)?(?:the\s+)?Misc\w*\.?(?:\s+Records?)?,?\s*(?:at\s+)?(?:p|P)\w*\.?\s*{_NUM}",
    rf"Misc\w*\.?\s*(?:Records?\s*)?Book\s*{_NUM},?\s*(?:at\s+)?(?:p|P)\w*\.?\s*{_NUM}",
]


def _good_line(line: str) -> bool:
    toks = line.split()
    if len(toks) < 3:
        return False
    words = [t for t in toks if re.fullmatch(r"[A-Za-z][A-Za-z'.,;:()\-]*", t) and len(t.strip(".,;:()")) >= 2]
    return len(words) / len(toks) >= 0.6


def _ocr_rotation(pdf_path: Path, rot: int) -> str:
    doc = fitz.open(pdf_path)
    lines = []
    for page in doc:
        pix = page.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72).prerotate(rot), colorspace=fitz.csGRAY)
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            pix.save(f.name)
            out = subprocess.run(["tesseract", f.name, "-", "--psm", "11"], capture_output=True, text=True,
                                 env={**os.environ, "OMP_THREAD_LIMIT": "1"}).stdout
        # psm 11 returns one text fragment per line; rejoin fragments into readable runs
        lines += [ln.strip() for ln in out.splitlines() if ln.strip()]
    return "\n".join(ln for ln in lines if _good_line(ln))


def ocr_plat(pdf_path: str) -> tuple[str, str]:
    """OCR one plat at each rotation; returns (pdf path, text)."""
    p = Path(pdf_path)
    parts = []
    for rot in ROTATIONS:
        text = _ocr_rotation(p, rot)
        if len(text.split()) >= 25:  # a rotation that reads produces runs of real words
            parts.append(f"[rotation {rot}]\n{text}")
    return pdf_path, "\n\n".join(parts)


def ocr_all(manifest: dict, workers: int = 6, echo=print) -> int:
    """OCR every downloaded plat image not yet OCR'd. Returns how many were done."""
    OCR_DIR.mkdir(parents=True, exist_ok=True)
    todo = []
    for e in manifest["instruments"]:
        img = plats.image_path(e)
        if img.exists() and not (OCR_DIR / f"{img.stem}.txt").exists():
            todo.append(str(img))
    echo(f"{len(todo)} plat images to OCR")
    done = 0
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(ocr_plat, t) for t in todo]
        for fut in as_completed(futures):
            path, text = fut.result()
            (OCR_DIR / f"{Path(path).stem}.txt").write_text(text)
            done += 1
            if done % 25 == 0 or done == len(todo):
                echo(f"  OCR {done}/{len(todo)}")
    return done


def _flatten(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\[rotation \d+\]", " ", text))


def find_restrictions(text: str) -> dict:
    flat = _flatten(text)
    terms = sorted(k for k, pat in RESTRICTION_TERMS.items() if re.search(pat, flat, re.I))
    citations = []
    for pat in CITATION_PATTERNS:
        for m in re.finditer(pat, flat, re.I):
            c = {"book": int(m.group(1)), "page": int(m.group(2)), "text": m.group(0)}
            if (c["book"], c["page"]) not in {(x["book"], x["page"]) for x in citations}:
                citations.append(c)
    # OCR often splits "page 306" away from "Book 22 of Miscellaneous Records", so also take
    # any "Book N ... Misc" and look a little further on for the page
    for m in re.finditer(r"Book\s*(\d{1,3})\s*(?:of\s+)?(?:the\s+)?Misc\w*", flat, re.I):
        book = int(m.group(1))
        if any(c["book"] == book for c in citations):
            continue
        after = flat[m.end(): m.end() + 160]
        pm = re.search(r"\b(?:p|pa|age|page|pages|pg)\.?\s*(\d{1,3})\b", after, re.I)
        citations.append({"book": book, "page": int(pm.group(1)) if pm else None,
                          "text": flat[m.start(): m.end() + 60], "uncertain": True})
    # a short excerpt from the first restriction paragraph, for review
    excerpt = None
    m = re.search(r"\b(?:restrictions?|covenants?|homes?\s+associat\w*)\b", flat, re.I)
    if m:
        excerpt = flat[max(0, m.start() - 60): m.start() + 500].strip()
    # mentions of "Misc"/"Miscellaneous" records the citation patterns didn't parse (OCR splits)
    misc_mentions = len(re.findall(r"\bMisc\w*", flat, re.I))
    return {"terms": terms, "citations": citations, "misc_mentions": misc_mentions, "excerpt": excerpt,
            "words": len(flat.split())}


EXCERPTS_PATH = OCR_DIR / "excerpts.md"  # local only: excerpts can carry signers' names


def build(manifest: dict) -> dict:
    """Scan the OCR text of every Prairie Village plat; write the checked-in results (terms and
    citations only) and a local excerpts file for reading."""
    from pvcouncil import ocrengines, transcripts

    manual, chosen = transcripts.load(), ocrengines.adopted()
    results, excerpts = [], ["# Restriction excerpts from plat OCR (local, not checked in)", ""]
    for e in manifest["instruments"]:
        if e["match"] not in ("exact", "variant"):
            continue
        txt = OCR_DIR / f"{plats.image_path(e).stem}.txt"
        if not txt.exists():
            continue
        tess = txt.read_text()
        # a reading from another engine, adopted in the viewer, stands in for Tesseract's
        ocr_text, eng = ocrengines.base_text(f"p:{e['id']}", tess, plats.image_path(e), chosen)
        r = find_restrictions(ocr_text + "\n\n" + transcripts.text_for(f"p:{e['id']}", manual))
        r["words"] = len(_flatten(tess).split())  # what Tesseract alone read
        if eng:
            base_terms = set(find_restrictions(tess)["terms"])
            r.update(engine=eng["label"], engine_stale=eng["stale"],
                     engine_terms=sorted(set(find_restrictions(ocr_text)["terms"]) - base_terms))
        items = manual.get(f"p:{e['id']}", [])
        r["transcribed"] = len(items)
        if items:
            # terms only a transcription found, cited to its page and region
            base = set(find_restrictions(ocr_text)["terms"])
            r["transcribed_terms"] = transcripts.attribute(items, base, lambda t: find_restrictions(t)["terms"])
            fp = transcripts.fingerprint(plats.image_path(e))
            r["stale_transcriptions"] = sum(transcripts.is_stale(x, fp) is True for x in items)
        excerpt = r.pop("excerpt")
        for c in r["citations"]:
            c.pop("text", None)
        results.append({"id": e["id"], "book_page": e["book_page"], "recorded": e["recorded"],
                        "plat": e["plat"], "subdivision_code": e["subdivision_code"], **r})
        if excerpt or r["citations"]:
            excerpts += [f"## {e['plat']} — {e['book_page']} ({e['recorded']})", "",
                         f"Terms: {', '.join(r['terms']) or 'none'}; citations: "
                         f"{', '.join(f'MISC {c['book']}/{c['page']}' for c in r['citations']) or 'none'}",
                         "", f"> {excerpt}" if excerpt else "", ""]
    out = {"plats": results}
    RESULTS_PATH.write_text(json.dumps(out, indent=1) + "\n")
    EXCERPTS_PATH.write_text("\n".join(excerpts))
    return out
