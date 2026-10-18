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
import queue
import re
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import fitz

from pvcouncil import ocrengines, platocr, plats, restrictread, transcripts

PAGE_CACHE = plats.DEEDS_DIR / "cache" / "pages"
HTML_PATH = Path(__file__).with_name("viewer.html")
DPI_STEPS = (72, 110, 160, 220, 300)
MAX_SIDE = 9000  # pixels; big plat sheets are capped so a render stays manageable
OCR_DPIS = (150, 200, 300, 400, 600)
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
            "transcribed_terms": o.get("transcribed_terms", {}),
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
                "transcribed_terms": r.get("transcribed_terms", {}),
            }
    from pvcouncil import deedchain

    if deedchain.RESULTS_PATH.exists():
        for r in json.loads(deedchain.RESULTS_PATH.read_text())["deeds"]:
            path = deedchain.image_path(r)
            if not path.exists() or f"d:{r['id']}" in docs:
                continue
            words = r.get("words", 0)
            docs[f"d:{r['id']}"] = {
                "key": f"d:{r['id']}", "kind": "deed", "book_page": r["book_page"], "recorded": r["recorded"],
                "title": f"{r['subdivision']} Block {r['block']}" + (f" Lot {r['lot']}" if r['lot'] else ""), "code": "",
                "subdivision": r["subdivision"], "path": str(path),
                "features": ["racial restriction"] if r["racial"] else [], "note": None, "term": None,
                "ocr_words": words, "ocr": "poor" if words / max(1, r["pages"]) < POOR_INSTRUMENT_WORDS_PER_PAGE else "good",
                "transcribed_terms": {},
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


def ocr_region(doc: dict, page: int, box: list[float], rot: int, dpi: int, invert: bool,
               threshold: bool, psm: int, engine: str = "tesseract") -> dict:
    """OCR one boxed region. box is [x0, y0, x1, y1] in fractions of the unrotated page; the
    crop is rendered upright at the given rotation, cleaned up, and read by the chosen engine
    (see ocrengines.py). Returns the text and a preview of the image the engine saw."""
    with fitz.open(doc["path"]) as pdf:
        p = pdf[page - 1]
        r = p.rect
        x0, y0, x1, y1 = (min(max(v, 0.0), 1.0) for v in box)
        clip = fitz.Rect(r.x0 + x0 * r.width, r.y0 + y0 * r.height, r.x0 + x1 * r.width, r.y0 + y1 * r.height)
        scale = dpi / 72
        if clip.width * clip.height * scale * scale > OCR_MAX_PIXELS:
            scale = (OCR_MAX_PIXELS / (clip.width * clip.height)) ** 0.5
        pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale).prerotate(rot), clip=clip, colorspace=fitz.csGRAY)
    img = ocrengines.prepare(pix, invert, threshold)
    e = ocrengines.get(engine)
    if e.max_side:
        img = ocrengines.fit(img, e.max_side)
    t0 = time.time()
    text = ocrengines.read(engine, [img], psm=psm, region=True)[0]
    w, h = img.width, img.height
    shrink = max(1, w // 900 + 1) if w > 900 else 1
    prev = fitz.Pixmap(img, w // shrink, h // shrink) if shrink > 1 else img
    return {
        "text": text, "words": ocrengines.words(text), "size": [w, h], "engine": engine,
        "label": e.label, "seconds": round(time.time() - t0, 1),
        "preview": "data:image/jpeg;base64," + base64.b64encode(prev.tobytes("jpg", jpg_quality=80)).decode(),
    }


def regions(doc: dict) -> list[dict]:
    """A document's saved regions, each marked stale if drawn on a different image."""
    fp = transcripts.fingerprint(doc["path"])
    return [{**x, "stale": transcripts.is_stale(x, fp)} for x in transcripts.for_key(doc["key"])]


def source(doc: dict) -> dict:
    return {"book_page": doc["book_page"], "recorded": doc["recorded"], "pages": doc["pages"],
            "fingerprint": transcripts.fingerprint(doc["path"])}


def tesseract_text(doc: dict) -> str:
    """The pipeline's own Tesseract OCR of a document."""
    from pvcouncil import deedchain

    d = {"p": platocr.OCR_DIR, "d": deedchain.OCR_DIR}.get(doc["key"][0], restrictread.OCR_DIR)
    f = d / f"{Path(doc['path']).stem}.txt"
    return f.read_text() if f.exists() else ""


def reclassify(doc: dict) -> dict:
    """Restriction terms for a document with its OCR text (an adopted engine reading, else
    Tesseract's) plus saved transcriptions, and which of them only a transcription found."""
    key = doc["key"]
    items = transcripts.for_key(key)
    ocr, eng = ocrengines.base_text(key, tesseract_text(doc), doc["path"])
    if key.startswith("p:"):
        terms = lambda t: platocr.find_restrictions(t)["terms"]  # noqa: E731
        return {"features": terms(ocr + "\n\n" + transcripts.text_for(key)),
                "transcribed_terms": transcripts.attribute(items, set(terms(ocr)), terms),
                "engine": eng, "engine_terms": _engine_terms(doc, eng, ocr, terms)}
    c = restrictread.classify(ocr + "\n\n" + transcripts.text_for(key))
    terms = lambda t: restrictread.classify(t)["features"]  # noqa: E731
    out = {"features": c["features"], "term": c["term"],
           "transcribed_terms": transcripts.attribute(items, set(terms(ocr)), terms),
           "engine": eng, "engine_terms": _engine_terms(doc, eng, ocr, terms)}
    if not doc.get("reviewed"):  # a kind read by eye stands
        out["kind"] = c["kind"]
    return out


def _engine_terms(doc: dict, eng: dict | None, text: str, terms) -> list[str]:
    """Terms an adopted engine reading found that Tesseract didn't."""
    return sorted(set(terms(text)) - set(terms(tesseract_text(doc)))) if eng else []


def readings(doc: dict) -> dict:
    """Every reading of a document: Tesseract's from the pipeline, and each engine's."""
    fp = transcripts.fingerprint(doc["path"])
    tess = tesseract_text(doc)
    parts = re.split(r"\[page (\d+)\]\n?", tess)
    if len(parts) > 1:
        pages = [{"page": int(n), "text": t.strip()} for n, t in zip(parts[1::2], parts[2::2])]
    else:  # plats: one sheet, read at several rotations
        pages = [{"page": 1, "text": tess.strip()}]
    out = [{"engine": "pipeline", "label": "Tesseract (pipeline)", "words": ocrengines.words(tess),
            "pages": pages, "stale": False}]
    for name, r in ocrengines.readings_for(doc["key"]).items():
        out.append({**{k: r[k] for k in ("engine", "label", "model", "ran", "seconds", "settings", "words", "pages")},
                    "stale": r.get("fingerprint") != fp})
    return {"readings": out, "adopted": ocrengines.adopted().get(doc["key"])}


class Jobs:
    """Whole-document readings, run one at a time in the background: the models share memory."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.jobs: dict[str, dict] = {}
        threading.Thread(target=self._run, daemon=True).start()

    def submit(self, doc: dict, engine: str, rot: int, invert: str) -> dict:
        for j in self.jobs.values():
            if j["key"] == doc["key"] and j["engine"] == engine and j["status"] in ("queued", "running"):
                return j
        j = {"id": uuid.uuid4().hex[:8], "key": doc["key"], "book_page": doc["book_page"], "engine": engine,
             "label": ocrengines.get(engine).label, "status": "queued", "done": 0, "total": doc["pages"],
             "error": None, "queued": time.time()}
        self.jobs[j["id"]] = j
        self.q.put((j, doc["path"], rot, invert))
        return j

    def _run(self):
        while True:
            j, path, rot, invert = self.q.get()
            j["status"], j["started"] = "running", time.time()
            try:
                ocrengines.read_document(j["engine"], j["key"], path, rot, invert,
                                         progress=lambda d, t: j.update(done=d, total=t))
                j["status"] = "done"
            except Exception as err:  # noqa: BLE001 - reported to the browser
                j["status"], j["error"] = "failed", str(err)[:300]
            j["finished"] = time.time()

    def list(self) -> list[dict]:
        cutoff = time.time() - 3600  # finished jobs drop off after an hour
        return [j for j in self.jobs.values() if j.get("finished", time.time()) > cutoff]


class Handler(BaseHTTPRequestHandler):
    docs: dict = {}
    jobs: Jobs | None = None

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
        manual, chosen = transcripts.load(), ocrengines.adopted()
        out = []
        for d in self.docs.values():
            items = manual.get(d["key"], [])
            stale, now = 0, {}
            if items or d["key"] in chosen:
                fp = transcripts.fingerprint(d["path"])
                stale = sum(transcripts.is_stale(x, fp) is True for x in items)
                now = reclassify(d)  # transcriptions saved since startup change the terms
            # the browser never sees local file paths
            out.append({**{k: v for k, v in d.items() if k != "path"}, **now,
                        "transcribed": len(items), "stale_transcriptions": stale,
                        "read_by": [n for n in ocrengines.ENGINES if ocrengines.reading_path(n, d["key"]).exists()]})
        return sorted(out, key=lambda d: (d["subdivision"], d["recorded"], d["book_page"]))

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            return self._send(HTML_PATH.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/docs":
            return self._json(self._public())
        if url.path == "/api/engines":
            return self._json(ocrengines.available(refresh="refresh" in q))
        if url.path == "/api/jobs":
            return self._json(self.jobs.list())
        key = q.get("key", [""])[0]
        if url.path == "/api/transcripts" and key in self.docs:
            return self._json(regions(self.docs[key]))
        if url.path == "/api/readings" and key in self.docs:
            return self._json(readings(self.docs[key]))
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
                engine = str(body.get("engine", "tesseract"))
                ocrengines.get(engine)
                try:
                    return self._json(ocr_region(doc, page, box, rot, dpi, bool(body.get("invert")),
                                                 bool(body.get("threshold")), psm, engine))
                except RuntimeError as err:
                    return self._json({"error": str(err)})
            if path == "/api/read-doc":
                engine, rot, invert = str(body["engine"]), int(body.get("rot", 0)), str(body.get("invert", "auto"))
                ocrengines.get(engine)
                if rot not in (0, 90, 180, 270) or invert not in ("auto", "on", "off"):
                    return self.send_error(HTTPStatus.BAD_REQUEST)
                if not ocrengines.available(refresh=True)[engine]["ok"]:
                    return self._json({"error": f"{engine} isn't set up: {ocrengines.available()[engine]['setup']}"})
                return self._json(self.jobs.submit(doc, engine, rot, invert))
            if path == "/api/adopt":
                engine = body.get("engine")
                ocrengines.adopt(doc["key"], str(engine) if engine else None)
                return self._json({"classified": reclassify(doc), **readings(doc)})
            if path == "/api/transcripts":
                region = body["region"]
                if not (1 <= int(region["page"]) <= doc["pages"] and len(region["box"]) == 4):
                    return self.send_error(HTTPStatus.BAD_REQUEST)
                saved = transcripts.save(doc["key"], region, source(doc))
                return self._json({"saved": {**saved, "stale": False}, "classified": reclassify(doc)})
            if path == "/api/transcripts/delete":
                transcripts.delete(doc["key"], str(body["id"]))
                return self._json({"classified": reclassify(doc)})
        except (ValueError, KeyError, TypeError):
            return self.send_error(HTTPStatus.BAD_REQUEST)
        self.send_error(HTTPStatus.NOT_FOUND)


def serve(port: int = 8765, echo=print) -> None:
    docs = build_index()
    Handler.docs = docs
    Handler.jobs = Jobs()
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
