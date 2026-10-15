"""A local web viewer for the recorded plats and restriction instruments.

`pvc deed-viewer` serves a one-page app on 127.0.0.1 that lists every downloaded
plat and every instrument filed with a plat, grouped by subdivision, and pages
through their images with zoom, pan, rotation, and inversion (for the
photostat negatives). Pages are rendered from the local PDFs on demand and
cached in deeds/cache/pages/. Nothing leaves the machine: the images name
signers and owners, so the server binds to localhost only.
"""

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import fitz

from pvcouncil import platocr, plats, restrictread

PAGE_CACHE = plats.DEEDS_DIR / "cache" / "pages"
HTML_PATH = Path(__file__).with_name("viewer.html")
DPI_STEPS = (72, 110, 160, 220, 300)
MAX_SIDE = 9000  # pixels; big plat sheets are capped so a render stays manageable


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
        docs[f"p:{e['id']}"] = {
            "key": f"p:{e['id']}", "kind": "plat", "book_page": e["book_page"], "recorded": e["recorded"],
            "title": " / ".join(pl), "code": code, "subdivision": sub, "path": str(path),
            "features": o.get("terms", []), "note": None, "term": None,
        }
    if restrictread.RESULTS_PATH.exists():
        for r in json.loads(restrictread.RESULTS_PATH.read_text())["read"]:
            if r.get("unrelated"):
                continue
            path = restrictread.image_path(r)
            if not path.exists():
                continue
            code, sub = subdivision(r["plats"])
            docs[f"r:{r['id']}"] = {
                "key": f"r:{r['id']}", "kind": r["kind"], "book_page": r["book_page"],
                "recorded": r["recorded"], "title": " / ".join(r["plats"]), "code": code, "subdivision": sub,
                "path": str(path), "features": r["features"], "note": r.get("note"), "term": r.get("term"),
                "filed_with": r["plat_book_pages"],
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


class Handler(BaseHTTPRequestHandler):
    docs: dict = {}
    public: list = []

    def log_message(self, *args):  # quiet
        pass

    def _send(self, body: bytes, ctype: str, cache: bool = False):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            return self._send(HTML_PATH.read_bytes(), "text/html; charset=utf-8")
        if url.path == "/api/docs":
            return self._send(json.dumps(self.public).encode(), "application/json")
        m = re.fullmatch(r"/page/([pr]):(\d+)/(\d+)\.jpg", url.path)
        if m:
            doc = self.docs.get(f"{m.group(1)}:{m.group(2)}")
            page = int(m.group(3))
            q = parse_qs(url.query)
            dpi = int(q.get("dpi", ["110"])[0])
            rot = int(q.get("rot", ["0"])[0])
            if doc and 1 <= page <= doc["pages"] and dpi in DPI_STEPS and rot in (0, 90, 180, 270):
                return self._send(render(doc, page, dpi, rot).read_bytes(), "image/jpeg", cache=True)
        self.send_error(HTTPStatus.NOT_FOUND)


def serve(port: int = 8765, echo=print) -> None:
    docs = build_index()
    Handler.docs = docs
    # the browser never sees local file paths
    Handler.public = sorted(({k: v for k, v in d.items() if k != "path"} for d in docs.values()),
                            key=lambda d: (d["subdivision"], d["recorded"], d["book_page"]))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    echo(f"{sum(d['kind'] == 'plat' for d in docs.values())} plats and "
         f"{sum(d['kind'] != 'plat' for d in docs.values())} instruments → http://127.0.0.1:{port}/  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

