"""Serve the subdivision map and the page images of the documents it links.

Besides the map directory's files, it serves docs/index.json (each linked document's page sizes in
points, keyed by its book and page as the map page slugs it: "MISC 43 573" → "misc-43-573") and
docs/<slug>/<page>-<size>.avif, each page as AVIF at two sizes: "full" (1600 px on its long side,
the scans' own resolution) and "thumb" (360 px). Grey AVIF at quality 35 is about a quarter the
size of JPEG at quality 80 and keeps typewritten text sharp.

Pages come from a pages directory (<slug>/<page>-<size>.avif and index.json, made by
`pvc map-bundle`), or are rendered from the documents' PDFs on first request and kept there.
The deploy image serves a pre-rendered pages directory with the standard library alone:
    python mapserve.py --map-dir map --pages-dir docs --host 0.0.0.0 --port 8080
Without documents, the index is empty and the map shows book and page numbers as text.
"""

import argparse
import functools
import http.server
import io
import json
import re
import urllib.parse
from pathlib import Path

SIDES = {"thumb": 360, "full": 1600}
AVIF = {"quality": 35, "speed": 6}


def index_docs(paths) -> dict[str, dict]:
    """Document PDFs by slug, with page sizes; a PDF named <slug>--<id>.pdf or <slug>.pdf.
    The first of several with one slug wins, and unreadable files are skipped."""
    import fitz

    out = {}
    for p in paths:
        slug = p.stem.split("--")[0].lower()
        if slug in out:
            continue
        try:
            with fitz.open(p) as pdf:
                out[slug] = {"path": p, "sizes": [[round(pg.rect.width), round(pg.rect.height)] for pg in pdf]}
        except fitz.FileDataError:
            continue
    return out


def page_path(pages_dir: Path, slug: str, page: int, size: str) -> Path:
    return pages_dir / slug / f"{page}-{size}.avif"


def render_page(pdf_path: Path, page: int, size: str) -> bytes:
    """One page as grey AVIF, scaled so its long side is SIDES[size]."""
    import fitz
    from PIL import Image

    with fitz.open(pdf_path) as pdf:
        p = pdf[page - 1]
        scale = SIDES[size] / max(p.rect.width, p.rect.height)
        pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY)
    buf = io.BytesIO()
    Image.frombytes("L", (pix.width, pix.height), pix.samples).save(buf, "AVIF", **AVIF)
    return buf.getvalue()


def write_page(pdf_path: Path, pages_dir: Path, slug: str, page: int, size: str) -> Path:
    out = page_path(pages_dir, slug, page, size)
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(f".{id(out)}.tmp")
        tmp.write_bytes(render_page(pdf_path, page, size))
        tmp.replace(out)
    return out


def handler(map_dir: Path, docs: dict[str, dict], pages_dir: Path):
    """docs: slug → {"sizes": [[w, h], ...], "path": its PDF, or None where pages_dir has the pages}."""
    index = json.dumps({s: d["sizes"] for s, d in docs.items()}, separators=(",", ":")).encode()

    class Handler(http.server.SimpleHTTPRequestHandler):
        extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map, ".avif": "image/avif"}

        def end_headers(self):  # so a rebuilt page or data shows on reload; page images don't change
            if self.path.endswith(".avif"):
                self.send_header("Cache-Control", "public, max-age=604800")
            else:
                self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def _send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlparse(self.path)
            if url.path == "/healthz":
                return self._send(b"ok", "text/plain")
            if url.path == "/docs/index.json":
                return self._send(index, "application/json")
            m = re.fullmatch(r"/docs/([a-z0-9-]+)/(\d+)-(thumb|full)\.avif", url.path)
            if m:
                slug, page, size = m.group(1), int(m.group(2)), m.group(3)
                doc = docs.get(slug)
                if doc and 1 <= page <= len(doc["sizes"]):
                    p = page_path(pages_dir, slug, page, size)
                    if not p.exists() and doc.get("path"):
                        p = write_page(doc["path"], pages_dir, slug, page, size)
                    if p.exists():
                        return self._send(p.read_bytes(), "image/avif")
            if url.path.startswith("/docs/"):
                return self.send_error(404)
            return super().do_GET()

    return functools.partial(Handler, directory=str(map_dir))


def serve(map_dir: Path, docs: dict[str, dict], pages_dir: Path, host: str, port: int) -> None:
    http.server.ThreadingHTTPServer((host, port), handler(map_dir, docs, pages_dir)).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--map-dir", type=Path, required=True)
    ap.add_argument("--pages-dir", type=Path, help="Pre-rendered pages: <slug>/<page>-<size>.avif and index.json")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    docs = {}
    if a.pages_dir and (a.pages_dir / "index.json").exists():
        docs = {s: {"sizes": sizes, "path": None}
                for s, sizes in json.loads((a.pages_dir / "index.json").read_text()).items()}
    print(f"{len(docs)} documents → http://{a.host}:{a.port}/", flush=True)
    serve(a.map_dir, docs, a.pages_dir or Path("."), a.host, a.port)
