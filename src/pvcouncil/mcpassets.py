"""Images and document lookups for the MCP server.

Every downloaded recorder image (plats, restriction instruments, pre-plat instruments, deed-chain
deeds) is addressed by its book and page as the recorder cites it ("MISC 26 349", "PLAT 12 41",
"DEED 230 425"). Council packets and minutes are addressed by filename and page, as search
results cite them. Pages are rendered as JPEG sized for a Claude client: a whole page at about
1568 px on its long side, or a region of it at a higher zoom, since a plat sheet's lettering is
unreadable when the whole sheet is shrunk to that size.

The images name signers and owners; they stay on this machine unless a client asks for them.
"""

import functools
import json
import math
import re
from pathlib import Path

import fitz

from pvcouncil import plats

DEFAULT_SIDE = 1568  # Claude downsizes anything larger, so larger costs bytes and reads no better
MAX_SIDE = 2400
MAX_SCALE = 300 / 72  # past 300 dpi a scan's own resolution runs out
KINDS = {"images": ("p", "plat"), "restrictions": ("r", "instrument"), "chain": ("d", "deed")}


def slug(book_page: str) -> str:
    return plats._slug(book_page)


@functools.lru_cache(maxsize=1)
def _doc_images() -> dict[str, dict]:
    from pvcouncil import subdivmap

    return subdivmap.doc_images()


def doc_images(refresh: bool = False) -> dict[str, dict]:
    if refresh:
        _doc_images.cache_clear()
    return _doc_images()


def find_doc(book_page: str) -> dict | None:
    """The downloaded image of a recorded document, with its viewer key ('p:<id>', 'r:<id>', 'd:<id>')."""
    d = doc_images().get(slug(book_page))
    if not d:
        return None
    path = Path(d["path"])
    letter, kind = KINDS.get(path.parent.name, ("r", "instrument"))
    rid = path.stem.split("--")[1] if "--" in path.stem else None
    return {"slug": slug(book_page), "path": path, "sizes": d["sizes"], "pages": len(d["sizes"]),
            "kind": kind, "key": f"{letter}:{rid}" if rid else None}


def _json(path: Path):
    return _json_cached(path, path.stat().st_mtime if path.exists() else None)


@functools.lru_cache(maxsize=32)
def _json_cached(path: Path, mtime):
    return json.loads(path.read_text()) if mtime is not None else None


def load(path: Path, default=None):
    v = _json(path)
    return default if v is None else v


def highlights(book_page: str) -> dict[int, list[list[float]]]:
    """Boxes round the racial language on a document's pages (fractions of the page), from map/highlights.json."""
    from pvcouncil import highlights as hl

    h = load(hl.RESULTS_PATH, {}).get(slug(book_page))
    return {int(p): boxes for p, boxes in (h or {}).get("pages", {}).items()}


def _jpeg(pix: fitz.Pixmap, quality: int = 80) -> bytes:
    return pix.tobytes("jpg", jpg_quality=quality)


def render(pdf_path: Path, page: int, *, region: list[float] | None = None, rotate: int = 0,
           invert: bool = False, boxes: list[list[float]] | None = None,
           max_side: int = DEFAULT_SIDE) -> tuple[bytes, dict]:
    """One page (1-based) as JPEG. region is [x0, y0, x1, y1] in fractions of the unrotated page,
    the convention the deed viewer and highlights use; boxes are outlined in red."""
    if rotate not in (0, 90, 180, 270):
        raise ValueError("rotate must be 0, 90, 180 or 270")
    max_side = max(200, min(max_side, MAX_SIDE))
    with fitz.open(pdf_path) as pdf:
        if not 1 <= page <= pdf.page_count:
            raise ValueError(f"page {page} out of range (1–{pdf.page_count})")
        p = pdf[page - 1]
        r = p.rect
        clip = r
        if region:
            x0, y0, x1, y1 = (min(1.0, max(0.0, v)) for v in region)
            if x1 <= x0 or y1 <= y0:
                raise ValueError("region must be [x0, y0, x1, y1] with x0 < x1 and y0 < y1, as fractions 0–1")
            clip = fitz.Rect(r.x0 + x0 * r.width, r.y0 + y0 * r.height, r.x0 + x1 * r.width, r.y0 + y1 * r.height)
        for b in boxes or []:
            # drawn on the page in memory (the file is not saved); cyan inverts to red
            rect = fitz.Rect(r.x0 + b[0] * r.width, r.y0 + b[1] * r.height,
                             r.x0 + b[2] * r.width, r.y0 + b[3] * r.height)
            p.draw_rect(rect, color=(0, 1, 1) if invert else (1, 0, 0), width=max(2, r.width / 300))
        scale = min(MAX_SCALE, max_side / max(clip.width, clip.height))
        pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale).prerotate(rotate), clip=clip,
                           colorspace=fitz.csRGB if boxes else fitz.csGRAY)
        if invert:
            pix.invert_irect()
        # how much of the scan's own resolution the image keeps, from the page's largest embedded image
        native = max((im[2] for im in p.get_images(full=True)), default=0)
        of_scan = pix.width / (native * clip.width / r.width) if native and rotate in (0, 180) else \
            pix.height / (native * clip.width / r.width) if native else None
        info = {"page": page, "pages": pdf.page_count, "width_px": pix.width, "height_px": pix.height,
                "of_scan": round(of_scan, 2) if of_scan else None}
    return _jpeg(pix), info


# ---------------------------------------------------------------------------
# Council packets and minutes
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def _council_files() -> dict[str, dict]:
    from pvcouncil.index import discover_docs

    return {d["filename"]: d for d in discover_docs()}


def council_doc(filename: str) -> dict | None:
    files = _council_files()
    name = Path(filename).name
    if name not in files:
        _council_files.cache_clear()
        files = _council_files()
    return files.get(name)


# ---------------------------------------------------------------------------
# Subdivision map
# ---------------------------------------------------------------------------

STATUS_COLORS = {  # the map's legend, roughly: racial covenants in red, released in green
    "racial": (200, 40, 40), "released": (60, 150, 80), "expired": (150, 150, 150),
    "reference": (230, 120, 60), "restrictions": (90, 120, 200), "none": (225, 225, 225),
}


def _rings(geom: dict):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        yield from poly


def subdivision_map(features: list[dict], focus: set[int] | None = None, parcels: list[dict] | None = None,
                    labels: bool = True, side: int = DEFAULT_SIDE) -> bytes:
    """A plain map of subdivision outlines colored by restriction status, as PNG. focus is the
    indexes of features to zoom to and outline; parcels (GeoJSON features) are drawn over them."""
    from PIL import Image, ImageDraw, ImageFont

    shown = [features[i] for i in focus] if focus else features
    pts = [c for f in shown for ring in _rings(f["geometry"]) for c in ring]
    lon0, lon1 = min(p[0] for p in pts), max(p[0] for p in pts)
    lat0, lat1 = min(p[1] for p in pts), max(p[1] for p in pts)
    pad = 0.15 if focus else 0.02
    dlon, dlat = (lon1 - lon0) or 1e-3, (lat1 - lat0) or 1e-3
    lon0, lon1, lat0, lat1 = lon0 - pad * dlon, lon1 + pad * dlon, lat0 - pad * dlat, lat1 + pad * dlat
    kx = math.cos(math.radians((lat0 + lat1) / 2))  # equirectangular, true to scale this far north
    w_deg, h_deg = (lon1 - lon0) * kx, lat1 - lat0
    s = side / max(w_deg, h_deg)
    W, H = max(1, round(w_deg * s)), max(1, round(h_deg * s))

    def xy(c):
        return ((c[0] - lon0) * kx * s, (lat1 - c[1]) * s)

    img = Image.new("RGB", (W, H), (250, 250, 247))
    d = ImageDraw.Draw(img, "RGBA")
    for i, f in enumerate(features):
        col = STATUS_COLORS.get(f["properties"].get("status"), (200, 200, 200))
        fill = (*col, 150 if not focus or i in focus else 60)
        for ring in _rings(f["geometry"]):
            d.polygon([xy(c) for c in ring], fill=fill, outline=(80, 80, 80, 200))
    for p in parcels or []:
        for ring in _rings(p["geometry"]):
            d.polygon([xy(c) for c in ring], outline=(40, 40, 40, 160))
    for i in focus or []:
        for ring in _rings(features[i]["geometry"]):
            d.line([xy(c) for c in ring] + [xy(ring[0])], fill=(0, 0, 0, 255), width=3)
    if labels:
        try:
            font = ImageFont.load_default(size=16 if focus else 11)
        except TypeError:
            font = ImageFont.load_default()
        from shapely.geometry import shape

        for i, f in enumerate(features):
            if focus and i not in focus:
                continue
            pt = shape(f["geometry"]).representative_point()
            x, y = xy((pt.x, pt.y))
            # Pillow's built-in font has no en dash
            text = (f["properties"]["name"] if focus else f["properties"]["code"]).replace("–", "-")
            d.text((x, y), text, fill=(0, 0, 0), font=font, anchor="mm", stroke_width=2, stroke_fill=(255, 255, 255))
    # legend
    y = 8
    for status, col in STATUS_COLORS.items():
        d.rectangle([8, y, 24, y + 14], fill=(*col, 200), outline=(60, 60, 60))
        d.text((30, y), status, fill=(0, 0, 0))
        y += 20
    import io

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def norm_book_page(s: str) -> str:
    """'misc 26/349', 'MISC 26-349', 'misc-26-349' → 'MISC 26 349'."""
    return re.sub(r"[\s/\-_,]+", " ", s.strip()).upper()
