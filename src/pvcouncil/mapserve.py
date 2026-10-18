"""Serve the subdivision map and the images of the documents it links.

Besides the map directory's files, it serves docs/index.json (each linked document's page sizes in
points, keyed by its book and page as the map page slugs it: "MISC 43 573" → "misc-43-573") and
docs/<slug>/<page>.jpg?size=thumb|full, rendered from the document's PDF on first request and
cached. Without documents, the index is empty and the map shows book and page numbers as text.

Depends only on PyMuPDF and the standard library, so the deploy image can run this file alone:
    python mapserve.py --map-dir map --docs-dir docs --host 0.0.0.0 --port 8080
where docs/ holds <slug>.pdf files (built by `pvc map-bundle`).
"""

import argparse
import functools
import http.server
import json
import re
import tempfile
import urllib.parse
from pathlib import Path

import fitz

PAGE_SIDES = {"thumb": 360, "full": 1800}


def index_docs(paths) -> dict[str, dict]:
    """Document PDFs by slug, with page counts and sizes; a PDF named <slug>--<id>.pdf or
    <slug>.pdf. The first of several with one slug wins, and unreadable files are skipped."""
    out = {}
    for p in paths:
        slug = p.stem.split("--")[0].lower()
        if slug in out:
            continue
        try:
            with fitz.open(p) as pdf:
                out[slug] = {"path": p, "pages": pdf.page_count,
                             "sizes": [[round(pg.rect.width), round(pg.rect.height)] for pg in pdf]}
        except fitz.FileDataError:
            continue
    return out


def render_page(doc: dict, page: int, size: str, cache_dir: Path) -> bytes:
    out = cache_dir / doc["path"].stem / f"{page}-{size}.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        with fitz.open(doc["path"]) as pdf:
            p = pdf[page - 1]
            scale = PAGE_SIDES[size] / max(p.rect.width, p.rect.height)
            pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY)
            tmp = out.with_suffix(f".{id(pix)}.tmp")
            tmp.write_bytes(pix.tobytes("jpg", jpg_quality=80))
            tmp.replace(out)
    return out.read_bytes()


def handler(map_dir: Path, docs: dict[str, dict], cache_dir: Path):
    index = json.dumps({s: d["sizes"] for s, d in docs.items()}, separators=(",", ":")).encode()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):  # so a rebuilt page or data shows on reload; page images don't change
            if re.match(r"/docs/[a-z0-9-]+/", self.path):
                self.send_header("Cache-Control", "public, max-age=86400")
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
            m = re.fullmatch(r"/docs/([a-z0-9-]+)/(\d+)\.jpg", url.path)
            if m:
                doc, page = docs.get(m.group(1)), int(m.group(2))
                size = urllib.parse.parse_qs(url.query).get("size", ["thumb"])[0]
                if doc and 1 <= page <= doc["pages"] and size in PAGE_SIDES:
                    return self._send(render_page(doc, page, size, cache_dir), "image/jpeg")
                return self.send_error(404)
            if url.path.startswith("/docs/"):  # the PDFs themselves aren't served
                return self.send_error(404)
            return super().do_GET()

    return functools.partial(Handler, directory=str(map_dir))


def serve(map_dir: Path, docs: dict[str, dict], cache_dir: Path, host: str, port: int) -> None:
    http.server.ThreadingHTTPServer((host, port), handler(map_dir, docs, cache_dir)).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--map-dir", type=Path, required=True)
    ap.add_argument("--docs-dir", type=Path, help="Directory of <slug>.pdf document images")
    ap.add_argument("--cache-dir", type=Path, default=Path(tempfile.gettempdir()) / "map-pages")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    a = ap.parse_args()
    docs = index_docs(sorted(a.docs_dir.glob("*.pdf"))) if a.docs_dir else {}
    print(f"{len(docs)} document images → http://{a.host}:{a.port}/", flush=True)
    serve(a.map_dir, docs, a.cache_dir, a.host, a.port)
