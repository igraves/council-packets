"""Where the racial language is on the pages of the documents the map links, so the map's document
viewer can outline it.

Each page of a document the map shows with a racial covenant is read with Tesseract's word boxes
(TSV), which place each line of type exactly. The pages where Tesseract finds racial wording
(deedchain.RACIAL), or every page of a document where it finds none (a poor scan or a photostat
negative), go to Claude with their numbered OCR lines; Claude reads the page and names the first and
last line of each paragraph with racial language, and the box is drawn round those lines. Where
the OCR lines are too garbled, Claude gives a box of its own. Claude's answers are cached in
deeds/cache/highlights/. If Claude finds nothing on a page where Tesseract found racial wording,
the wording's lines are grown to their paragraph from the page layout (an indented line, or a
wider gap than usual, starts a paragraph).

map/highlights.json holds only boxes, as fractions of the page: {slug: {"by": [engines],
"pages": {page: [[x0, y0, x1, y1], ...]}}}. No text from the documents goes in it.
"""

import base64
import csv
import io
import json
import os
import re
import statistics
import subprocess
import tempfile

import fitz

from pvcouncil import deedchain, ocrengines, plats, subdivmap

CACHE_DIR = plats.DEEDS_DIR / "cache" / "highlights"
RESULTS_PATH = subdivmap.MAP_DIR / "highlights.json"
DPI = 250
PAD = 0.006
# a line starting this far (fraction of page width) right of the text's left margin begins a paragraph
INDENT = 0.025
CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_SIDE = 1568
PROMPT = """This is one page of a recorded deed restriction or declaration from Johnson County, Kansas.
Find every paragraph on this page that contains racially restrictive language (for example clauses
barring ownership or occupancy by Black people, "negroes", "persons of color", "African descent",
or anyone not "of the white race", or similar).

Below the image are the page's lines as OCR read them, numbered top to bottom. The OCR may be
garbled, and lines may be missing or merged; trust the image for what the page says.

Answer with JSON only, no other text:
{"paragraphs": [{"lines": [first, last]}, ...]}
giving for each such paragraph the numbers of its first and last OCR line: the whole paragraph or
lettered/numbered item, and its heading too when the heading itself names race (e.g. "OWNERSHIP BY
NEGROES PROHIBITED"), but no neighbouring paragraph or item. Where the racial language is only a
sentence or two inside a long paragraph about other matters, give just the lines of those
sentences. If the OCR lines are too garbled to
find the paragraph in them, give {"box": [x0, y0, x1, y1]} for it instead: its bounding box as
fractions of the image's width and height (0 to 1, origin top left).
Use {"paragraphs": []} if there is none.

OCR lines:
"""


def racial_docs() -> list[str]:
    """Book and page of every document the built map shows with a racial covenant."""
    refs = set()
    for f in json.loads(subdivmap.GEOJSON_PATH.read_text())["features"]:
        refs |= {x["book_page"] for x in json.loads(f["properties"]["details"])["declarations"] if x["racial"]}
    for name in ("preplat.geojson", "carried.geojson"):
        for f in json.loads((subdivmap.MAP_DIR / name).read_text())["features"]:
            refs.add(f["properties"].get("book_page") or f["properties"].get("declaration"))
    return sorted(r for r in refs if r)


def _pixmap(page, invert: bool) -> fitz.Pixmap:
    pix = page.get_pixmap(matrix=fitz.Matrix(DPI / 72, DPI / 72), colorspace=fitz.csGRAY)
    if invert:
        pix.invert_irect()
    return pix


def page_lines(page) -> list[dict]:
    """The page's lines of type, top to bottom, as Tesseract reads them: text and box as fractions
    of the page. A photostat negative is read inverted."""
    pix = _pixmap(page, invert=False)
    if ocrengines.is_negative(pix):
        pix.invert_irect()
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        pix.save(f.name)
        out = subprocess.run(["tesseract", f.name, "-", "tsv"], capture_output=True, text=True,
                             env={**os.environ, "OMP_THREAD_LIMIT": "1"}).stdout
    lines = {}
    for r in csv.DictReader(io.StringIO(out), delimiter="\t", quoting=csv.QUOTE_NONE):
        if r.get("level") != "5" or not (r.get("text") or "").strip():
            continue
        k = (r["block_num"], r["par_num"], r["line_num"])
        x, y, w, h = (int(float(r[c])) for c in ("left", "top", "width", "height"))
        ln = lines.setdefault(k, {"words": [], "x0": x, "y0": y, "x1": x + w, "y1": y + h})
        ln["words"].append(r["text"])
        ln.update(x0=min(ln["x0"], x), y0=min(ln["y0"], y), x1=max(ln["x1"], x + w), y1=max(ln["y1"], y + h))
    out = []
    for ln in lines.values():
        text = " ".join(ln["words"])
        if len(re.findall(r"[A-Za-z]", text)) < 3:  # specks, hole punches, margin marks
            continue
        out.append({"text": text, "x0": ln["x0"] / pix.width, "y0": ln["y0"] / pix.height,
                    "x1": ln["x1"] / pix.width, "y1": ln["y1"] / pix.height})
    return sorted(out, key=lambda ln: ln["y0"])


def hit_lines(lines: list[dict]) -> set[int]:
    """Lines holding racial wording, including wording broken across two lines."""
    text, starts = "", []
    for ln in lines:
        starts.append(len(text))
        text += ln["text"] + " "
    hits = set()
    for m in deedchain.RACIAL.finditer(text):
        hits |= {i for i, s in enumerate(starts) if s <= m.end() and m.start() < s + len(lines[i]["text"]) + 1}
    return hits


def paragraph_boxes(lines: list[dict], seeds: set[int]) -> list[list[float]]:
    """Each seed line grown to its paragraph: up to the nearest line that starts one (indented, or
    after a wide gap), down to the line before the next paragraph's start."""
    if not seeds:
        return []
    body = [ln for ln in lines if ln["x1"] - ln["x0"] > 0.3]  # full lines set the margin and spacing
    margin = statistics.median(ln["x0"] for ln in body) if body else min(ln["x0"] for ln in lines)
    gaps = [b["y0"] - a["y1"] for a, b in zip(lines, lines[1:]) if b["y0"] > a["y1"]]
    usual = statistics.median(gaps) if gaps else 0.0
    height = statistics.median(ln["y1"] - ln["y0"] for ln in lines)

    def starts_paragraph(i: int) -> bool:
        if i == 0:
            return True
        gap = lines[i]["y0"] - lines[i - 1]["y1"]
        return lines[i]["x0"] > margin + INDENT or gap > usual + 0.6 * height

    spans = set()
    for s in seeds:
        a = s
        while not starts_paragraph(a):
            a -= 1
        b = s
        while b + 1 < len(lines) and not starts_paragraph(b + 1):
            b += 1
        spans.add((a, b))
    merged = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1] + 1:  # a racial heading over its racial paragraph: one box
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [_box(lines[a:b + 1]) for a, b in merged]


def _box(lines: list[dict]) -> list[float]:
    return [round(max(0.0, min(ln["x0"] for ln in lines) - PAD), 4), round(max(0.0, min(ln["y0"] for ln in lines) - PAD), 4),
            round(min(1.0, max(ln["x1"] for ln in lines) + PAD), 4), round(min(1.0, max(ln["y1"] for ln in lines) + PAD), 4)]


def claude_boxes(page, lines: list[dict], cache_key: str) -> list[list[float]]:
    """Boxes for the paragraphs with racial language on this page: Claude names each one's first
    and last OCR line, whose positions Tesseract gives (or a box of its own where the OCR is too
    garbled). Claude's answers are cached."""
    cached = CACHE_DIR / f"{cache_key}.json"
    if not cached.exists():
        found = _ask_claude(page, lines)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps({"model": CLAUDE_MODEL, "lines": len(lines), "paragraphs": found}))
    boxes = []
    for p in json.loads(cached.read_text())["paragraphs"]:
        span = p.get("lines")
        if span and len(span) == 2 and 0 <= span[0] <= span[1] < len(lines):
            boxes.append(_box(lines[span[0]:span[1] + 1]))
        elif len(p.get("box") or []) == 4:
            boxes.append([round(min(1.0, max(0.0, v)), 4) for v in p["box"]])
    return boxes


def _ask_claude(page, lines: list[dict]) -> list[dict]:
    import anthropic

    from pvcouncil.config import ANTHROPIC_API_KEY

    pix = ocrengines.fit(_pixmap(page, invert=False), CLAUDE_SIDE)
    if ocrengines.is_negative(pix):
        pix.invert_irect()
    msg = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
        model=CLAUDE_MODEL, max_tokens=1000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(pix.tobytes("jpg", jpg_quality=85)).decode()}},
            {"type": "text", "text": PROMPT + "\n".join(f"{i}: {ln['text']}" for i, ln in enumerate(lines))}]}])
    raw = "".join(b.text for b in msg.content if b.type == "text")
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0))["paragraphs"] if m else []


def build(echo=print) -> dict:
    images = subdivmap.doc_images()
    out = {}
    for ref in racial_docs():
        slug = plats._slug(ref)
        if slug not in images:
            echo(f"{ref}: no image")
            continue
        pages, by = {}, set()
        with fitz.open(images[slug]["path"]) as pdf:
            lines = {n: page_lines(page) for n, page in enumerate(pdf, 1)}
            hits = {n: h for n, ls in lines.items() if (h := hit_lines(ls))}
            # Claude reads the pages Tesseract found racial wording on, or every page when it found none
            for n in hits or lines:
                if b := claude_boxes(pdf[n - 1], lines[n], f"{slug}-{n}"):
                    pages[n] = b
                    by |= {CLAUDE_MODEL, "Tesseract"}
                elif n in hits:
                    pages[n] = paragraph_boxes(lines[n], hits[n])
                    by.add("Tesseract")
        echo(f"{ref}: {sum(len(b) for b in pages.values())} boxes on page(s) "
             f"{', '.join(map(str, pages)) or 'none'} ({', '.join(sorted(by)) or '-'})")
        if pages:
            out[slug] = {"by": sorted(by), "pages": pages}
    RESULTS_PATH.write_text(json.dumps(out, indent=1) + "\n")
    return out
