"""A local web viewer for the recorded plats and restriction instruments.

`pvc deed-viewer` serves a one-page app on 127.0.0.1 that lists every downloaded
plat and every instrument filed with a plat, grouped by subdivision, and pages
through their images with zoom, pan, rotation, and inversion (for the
photostat negatives). Pages are rendered from the local PDFs on demand and
cached in deeds/cache/pages/. Nothing leaves the machine: the images name
signers and owners, so the server binds to localhost only.

Documents OCR read poorly are flagged. For those, a region of a page can be
boxed and OCR'd again at a higher zoom with clean-up (invert, contrast stretch,
threshold); the corrected text is saved as a transcription (see transcripts.py)
and the document re-classified with it.
"""

import base64
import json
import re
import subprocess
import tempfile
from collections import Counter
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import fitz

from pvcouncil import platocr, plats, restrictread, transcripts

PAGE_CACHE = plats.DEEDS_DIR / "cache" / "pages"
HTML_PATH = Path(__file__).with_name("viewer.html")
DPI_STEPS = (72, 110, 160, 220, 300)
MAX_SIDE = 9000  # pixels; big plat sheets are capped so a render stays manageable
OCR_DPIS = (200, 300, 400, 600)
OCR_PSMS = (3, 4, 6, 7, 11)
OCR_MAX_PIXELS = 40_000_000
# below these, OCR missed most of a document's text
POOR_PLAT_WORDS = 150
POOR_INSTRUMENT_WORDS_PER_PAGE = 120


def build_index() -> dict:
    """Every viewable document, keyed 'p:<id>' (plat) or 'r:<id>' (restriction instrument)."""
    manifest = plats.load_manifest()
    names = manifest["plats"]
    on_plat = {}
    if platocr.RESULTS_PATH.exists():
        on_plat = {p["id"]: p for p in json.loads(platocr.RESULTS_PATH.read_text())["plats"]}

    def subdivision(plat_names: list[str]) -> tuple[str, str]:
        for n in plat_names:
            if n in names:
                return names[n]["subdivision_code"], names[n]["subdivision"]
        return "", "Other"

    docs = {}
    for e in manifest["instruments"]:
        if e["match"] not in ("exact", "variant"):
            continue
        path = plats.image_path(e)
        if not path.exists():
            continue
        pl = e.get("plats") or [e["plat"]]
        code, sub = subdivision(pl)
        o = on_plat.get(e["id"], {})
        words = o.get("words", 0)
        docs[f"p:{e['id']}"] = {
            "key": f"p:{e['id']}", "kind": "plat", "book_page": e["book_page"], "recorded": e["recorded"],
            "title": " / ".join(pl), "code": code, "subdivision": sub, "path": str(path),
            "features": o.get("terms", []), "note": None, "term": None,
            "ocr_words": words, "ocr": "poor" if words < POOR_PLAT_WORDS else "good",
        }
    if restrictread.RESULTS_PATH.exists():
        for r in json.loads(restrictread.RESULTS_PATH.read_text())["read"]:
            if r.get("unrelated"):
                continue
            path = restrictread.image_path(r)
            if not path.exists():
                continue
            code, sub = subdivision(r["plats"])
            words = r.get("words", 0)
            poor = r.get("reviewed") or words / max(1, r["pages"]) < POOR_INSTRUMENT_WORDS_PER_PAGE
            docs[f"r:{r['id']}"] = {
                "key": f"r:{r['id']}", "kind": r["kind"], "book_page": r["book_page"],
                "recorded": r["recorded"], "title": " / ".join(r["plats"]), "code": code, "subdivision": sub,
                "path": str(path), "features": r["features"], "note": r.get("note"), "term": r.get("term"),
                "filed_with": r["plat_book_pages"], "reviewed": bool(r.get("reviewed")),
                "ocr_words": words, "ocr": "poor" if poor else "good",
            }
    for d in docs.values():
        with fitz.open(d["path"]) as pdf:
            d["pages"] = pdf.page_count
            # page sizes in points, so the browser can pick a render resolution for its zoom
            d["dims"] = [[round(pg.rect.width, 1), round(pg.rect.height, 1)] for pg in pdf]
    return docs


def render(doc: dict, page: int, dpi: int, rot: int) -> Path:
    out = PAGE_CACHE / doc["key"].replace(":", "-") / f"{page}-{dpi}-{rot}.jpg"
    if out.exists():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(doc["path"]) as pdf:
        p = pdf[page - 1]
        scale = dpi / 72
        side = max(p.rect.width, p.rect.height) * scale
        if side > MAX_SIDE:
            scale *= MAX_SIDE / side
        pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale).prerotate(rot), colorspace=fitz.csGRAY)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(pix.tobytes("jpg", jpg_quality=82))
        tmp.replace(out)
    return out


def _otsu(hist: list[int]) -> int:
    total = sum(hist)
    sum_all = sum(i * c for i, c in enumerate(hist))
    best, best_t, w0, sum0 = -1.0, 128, 0, 0
    for t in range(256):
        w0 += hist[t]
        if w0 == 0 or w0 == total:
            continue
        sum0 += t * hist[t]
        m0, m1 = sum0 / w0, (sum_all - sum0) / (total - w0)
        between = w0 * (total - w0) * (m0 - m1) ** 2
        if between > best:
            best, best_t = between, t
    return best_t


def ocr_region(doc: dict, page: int, box: list[float], rot: int, dpi: int, invert: bool,
               threshold: bool, psm: int) -> dict:
    """OCR one boxed region. box is [x0, y0, x1, y1] in fractions of the unrotated page; the
    crop is rendered upright at the given rotation, cleaned up, and handed to Tesseract.
    Returns the text and a preview of the image Tesseract saw."""
    with fitz.open(doc["path"]) as pdf:
        p = pdf[page - 1]
        r = p.rect
        x0, y0, x1, y1 = (min(max(v, 0.0), 1.0) for v in box)
        clip = fitz.Rect(r.x0 + x0 * r.width, r.y0 + y0 * r.height, r.x0 + x1 * r.width, r.y0 + y1 * r.height)
        scale = dpi / 72
        if clip.width * clip.height * scale * scale > OCR_MAX_PIXELS:
            scale = (OCR_MAX_PIXELS / (clip.width * clip.height)) ** 0.5
        pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale).prerotate(rot), clip=clip, colorspace=fitz.csGRAY)
    w, h = pix.width, pix.height
    data = bytes(pix.samples)
    if pix.stride != w:  # drop any row padding
        data = b"".join(data[i * pix.stride: i * pix.stride + w] for i in range(h))
    if invert:
        data = data.translate(bytes(255 - v for v in range(256)))
    # stretch levels between the 1st and 99th percentile; faint photostats use a narrow band
    hist = [0] * 256
    for v, n in Counter(data[:: max(1, len(data) // 400_000)]).items():
        hist[v] = n
    total, acc, lo, hi = sum(hist), 0, 0, 255
    for v in range(256):
        acc += hist[v]
        if acc <= total * 0.01:
            lo = v
        if acc <= total * 0.99:
            hi = v
    if hi - lo > 8:
        lut = bytes(min(255, max(0, (v - lo) * 255 // (hi - lo))) for v in range(256))
        data = data.translate(lut)
        hist = [sum(hist[u] for u in range(256) if lut[u] == v) for v in range(256)]
    if threshold:
        t = _otsu(hist)
        data = data.translate(bytes(255 if v > t else 0 for v in range(256)))
    img = fitz.Pixmap(fitz.csGRAY, w, h, data, False)
    with tempfile.NamedTemporaryFile(suffix=".png") as f:
        img.save(f.name)
        text = subprocess.run(["tesseract", f.name, "-", "--psm", str(psm), "-c", "preserve_interword_spaces=1"],
                              capture_output=True, text=True).stdout.strip()
    shrink = max(1, w // 900 + 1) if w > 900 else 1
    prev = fitz.Pixmap(img, w // shrink, h // shrink) if shrink > 1 else img
    return {
        "text": text, "words": len(re.findall(r"\b[A-Za-z]{3,}\b", text)), "size": [w, h],
        "preview": "data:image/jpeg;base64," + base64.b64encode(prev.tobytes("jpg", jpg_quality=80)).decode(),
    }


def reclassify(doc: dict) -> dict:
    """Restriction terms for a document with its OCR text plus saved transcriptions."""
    key = doc["key"]
    stem = Path(doc["path"]).stem
    if key.startswith("p:"):
        f = platocr.OCR_DIR / f"{stem}.txt"
        text = (f.read_text() if f.exists() else "") + "\n\n" + transcripts.text_for(key)
        return {"features": platocr.find_restrictions(text)["terms"]}
    f = restrictread.OCR_DIR / f"{stem}.txt"
    text = (f.read_text() if f.exists() else "") + "\n\n" + transcripts.text_for(key)
    c = restrictread.classify(text)
    out = {"features": c["features"], "term": c["term"]}
    if not doc.get("reviewed"):  # a kind read by eye stands
        out["kind"] = c["kind"]
    return out


class Handler(BaseHTTPRequestHandler):
    docs: dict = {}

    def log_message(self, *args):  # quiet
        pass

    def _send(self, body: bytes, ctype: str, cache: bool = False):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(json.dumps(obj).encode(), "application/json")

    def _public(self) -> list[dict]:
        manual = transcripts.load()
        # the browser never sees local file paths
        out = [{**{k: v for k, v in d.items() if k != "path"}, "transcribed": len(manual.get(d["key"], []))}
               for d in self.docs.values()]
        return sorted(out, key=lambda d: (d["subdivision"], d["recorded"], d["book_page"]))

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            return self._send(HTML_PATH.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/docs":
            return self._json(self._public())
        if url.path == "/api/transcripts":
            key = q.get("key", [""])[0]
            if key in self.docs:
                return self._json(transcripts.for_key(key))
        m = re.fullmatch(r"/page/([pr]):(\d+)/(\d+)\.jpg", url.path)
        if m:
            doc = self.docs.get(f"{m.group(1)}:{m.group(2)}")
            page = int(m.group(3))
            dpi = int(q.get("dpi", ["110"])[0])
            rot = int(q.get("rot", ["0"])[0])
            if doc and 1 <= page <= doc["pages"] and dpi in DPI_STEPS and rot in (0, 90, 180, 270):
                return self._send(render(doc, page, dpi, rot).read_bytes(), "image/jpeg", cache=True)
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self):
        # same-origin only: a page on another site can't drive this server
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            return self.send_error(HTTPStatus.FORBIDDEN)
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
            doc = self.docs[body["key"]]
        except (ValueError, KeyError):
            return self.send_error(HTTPStatus.BAD_REQUEST)
        path = urlparse(self.path).path
        try:
            if path == "/api/ocr":
                page, rot, dpi, psm = int(body["page"]), int(body["rot"]), int(body["dpi"]), int(body["psm"])
                box = [float(v) for v in body["box"]]
                if not (1 <= page <= doc["pages"] and rot in (0, 90, 180, 270) and dpi in OCR_DPIS
                        and psm in OCR_PSMS and len(box) == 4 and box[2] > box[0] and box[3] > box[1]):
                    return self.send_error(HTTPStatus.BAD_REQUEST)
                return self._json(ocr_region(doc, page, box, rot, dpi, bool(body.get("invert")),
                                             bool(body.get("threshold")), psm))
            if path == "/api/transcripts":
                region = body["region"]
                if not (1 <= int(region["page"]) <= doc["pages"] and len(region["box"]) == 4):
                    return self.send_error(HTTPStatus.BAD_REQUEST)
                saved = transcripts.save(doc["key"], region)
                return self._json({"saved": saved, "classified": reclassify(doc)})
            if path == "/api/transcripts/delete":
                transcripts.delete(doc["key"], str(body["id"]))
                return self._json({"classified": reclassify(doc)})
        except (ValueError, KeyError, TypeError):
            return self.send_error(HTTPStatus.BAD_REQUEST)
        self.send_error(HTTPStatus.NOT_FOUND)


def serve(port: int = 8765, echo=print) -> None:
    docs = build_index()
    Handler.docs = docs
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    poor = sum(d["ocr"] == "poor" for d in docs.values())
    echo(f"{sum(d['kind'] == 'plat' for d in docs.values())} plats and "
         f"{sum(d['kind'] != 'plat' for d in docs.values())} instruments ({poor} with poor OCR) → "
         f"http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
