"""A map of Prairie Village's subdivisions and their recorded restrictions.

Subdivision outlines are built from the county's public Prairie Village parcel layer
(maps.jocogov.org, PrairieVillage/Parcels): a platted parcel's ID reads OP<subdivision
code><block> <lot>, so the parcels of each subdivision code are merged into one shape,
closing the streets between blocks. Only parcel IDs and geometry are fetched; the
layer's owner fields are not. The raw parcels are cached in deeds/cache/gis/.

Each subdivision carries what the restrictions survey found: declarations filed with
its plats or found in the Miscellaneous index (with their terms, the blocks they cover,
and whether a racial covenant was released or ended), restrictions written on the plat,
and how many indexed restriction instruments name it. A subdivision is drawn in parts
where its racial covenants cover different blocks. The result, map/subdivisions.geojson,
is drawn by map/index.html.
"""

import json
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

from pvcouncil import platocr, plats, restrictions, restrictread
from pvcouncil.config import PROJECT_ROOT

PARCELS_URL = "https://maps.jocogov.org/ArcGIS/rest/services/PrairieVillage/Parcels/MapServer/0/query"
CACHE_PATH = plats.DEEDS_DIR / "cache" / "gis" / "pv-parcels.geojson"
MAP_DIR = PROJECT_ROOT / "map"
GEOJSON_PATH = MAP_DIR / "subdivisions.geojson"
REPORT_URL = "https://github.com/igraves/council-packets/blob/main/subdivision-restrictions.md"
PAGE = 2000
# streets between blocks are separate right-of-way parcels; grow each parcel by about
# half a street's width, merge, and shrink back so a subdivision is one shape
CLOSE_DEG = 0.00013
SIMPLIFY_DEG = 0.00002
# Each declaration covers the blocks it names (restrictread.lots_blocks, or restriction_review.json
# read by eye), or every block if it names none. Walk candidates (declarations found in the
# Miscellaneous index rather than filed with a plat) are shown only where they name blocks.
DECL_KINDS = {"restrictions", "restriction amendment", "homes association"}
# Lots re-platted into a later subdivision: a replat does not release the original
# declaration, so they are drawn with the subdivision and block they were first platted in.
REPLATS = {
    # Prairie Fields (PLAT 17 46, 1954) re-platted Lots 1-5, Block 1 of Meadowbrook Downs as its
    # Block 22, Lots 1-5; MISC 43 168 (1949) covers all six lots and renews every ten years
    "2350": {"code": "4200", "block": 22, "lots": {1, 2, 3, 4, 5}, "into_block": 1,
             "note": "Lots 1–5 were re-platted in 1954 as Prairie Fields Block 22, Lots 1–5 (PLAT 17 46). "
                     "The replat did not release the 1949 declaration, which renews every ten years.",
             "later_note": "Block 22, Lots 1–5 are shown with Meadowbrook Downs, the 1949 subdivision "
                           "they were re-platted from, whose declaration still covers them."},
}


# Re-platted land that does not line up with the new plat's lots, drawn from the plats' measurements
# as its own overlay (map/carried.geojson). "east_of": a strip ft wide along the east side of the
# named lots' parcels, from below_of's south line (or the lots' south line, rising) for len_ft.
CARRIED_PATH = PROJECT_ROOT / "map" / "carried.geojson"
FT_LAT = 1 / 364_000  # degrees of latitude per foot, near 39° N
CARRIED = [
    {"declaration": "MISC 32 195", "from": "Fonticello Gardens No. 2 (1946), Lot 7", "into": "Roe-69 (1983)",
     "east_of": ("1000", None), "width_ft": 100, "len_ft": 332.44, "rising": True,
     "note": "Lot 7, the east 100 feet of the 1946 plat, was left out of the 1959 Replat of Fonticello Gardens "
             "No. 2 and platted into Roe-69 in 1983 (PLAT 54 7). The 1946 declaration names Lot 7 and renews "
             "every 25 years."},
    {"declaration": "MISC 24 273", "from": "Prairie Woods (1939), east 70 feet of Lot 27", "into": "Fonticello (1982)",
     "east_of": ("5600", "32"), "below_of": ("5600", "26"), "width_ft": 70, "len_ft": 186.99, "rising": False,
     "note": "The east 70 feet of Lot 27 were resubdivided into Fonticello in 1982 (PLAT 52 24). The 1939 "
             "declaration covers every lot of Prairie Woods and renews automatically."},
]


def carried_overlay(parcels: dict, subs: dict) -> dict:
    from math import cos, radians

    import shapely
    from shapely.geometry import box, shape
    from shapely.ops import unary_union

    def lots(code, lot=None):
        return unary_union([shape(f["geometry"]) for f in parcels["features"]
                            if (pid := (f["properties"] or {}).get("GeoPropID") or "").startswith("OP" + code)
                            and (lot is None or pid[11:].lstrip("0").startswith(lot))])

    decls = {d["book_page"]: d for s in subs.values() for d in s["declarations"]}
    feats = []
    for i, c in enumerate(CARRIED):
        ref = lots(*c["east_of"])
        x0, y0, x1, y1 = ref.bounds
        ft_lon = FT_LAT / cos(radians(y0))
        top = lots(*c["below_of"]).bounds[1] if c.get("below_of") else None
        ylo, yhi = (y0, y0 + c["len_ft"] * FT_LAT) if c["rising"] else (top - c["len_ft"] * FT_LAT, top)
        area = box(x1, ylo, x1 + c["width_ft"] * ft_lon, yhi)
        d = decls.get(c["declaration"], {})
        props = {k: c[k] for k in ("declaration", "from", "into", "note")}
        props.update(recorded=d.get("recorded"), status=_status({"declarations": [d], "on_plat": [], "indexed": 0})
                     if d else "racial")
        feats.append({"type": "Feature", "id": i, "properties": props,
                      "geometry": json.loads(shapely.to_geojson(area))})
    fc = {"type": "FeatureCollection", "features": feats}
    CARRIED_PATH.write_text(json.dumps(fc, separators=(",", ":")))
    return fc


def fetch_parcels(refresh: bool = False, echo=print) -> dict:
    """Every parcel polygon in the layer (ID and geometry only), in WGS84."""
    if CACHE_PATH.exists() and not refresh:
        return json.loads(CACHE_PATH.read_text())
    features, offset = [], 0
    while True:
        q = urllib.parse.urlencode({
            "where": "1=1", "outFields": "GeoPropID", "returnGeometry": "true", "outSR": 4326,
            "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": PAGE, "f": "geojson"})
        req = urllib.request.Request(f"{PARCELS_URL}?{q}", headers={"User-Agent": "Mozilla/5.0 (council-packets)"})
        with urllib.request.urlopen(req, timeout=120) as r:
            page = json.load(r)
        features += page["features"]
        echo(f"{len(features)} parcels")
        if len(page["features"]) < PAGE:
            break
        offset += PAGE
    data = {"type": "FeatureCollection", "features": features}
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data))
    return data


def outlines(parcels: dict, group) -> dict:
    """Shape key (group(parcel ID)) → merged (Multi)Polygon of its platted parcels."""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    by_key = defaultdict(list)
    for f in parcels["features"]:
        pid = (f["properties"] or {}).get("GeoPropID") or ""
        if pid.startswith("OP") and f.get("geometry"):
            by_key[group(pid)].append(shape(f["geometry"]).buffer(0))
    out = {}
    for key, geoms in by_key.items():
        merged = unary_union([g.buffer(CLOSE_DEG, join_style="mitre") for g in geoms])
        out[key] = _fill_small_holes(merged.buffer(-CLOSE_DEG, join_style="mitre")).simplify(SIMPLIFY_DEG)
    return out


def code_block(pid: str) -> tuple[str, int | None]:
    """A parcel ID's subdivision code and block, with REPLATS lots moved back to the subdivision and
    block they were first platted in. A few blocks are lettered (00BS): None."""
    code = pid[2:6]
    block = int(pid[6:10]) if pid[6:10].isdigit() else None
    lot = re.match(r"\d*", pid[11:]).group()
    for orig, r in REPLATS.items():
        if code == r["code"] and block == r["block"] and lot and int(lot) in r["lots"]:
            return orig, r["into_block"]
    return code, block


def _ranges(nums) -> str:
    out = []
    for n in sorted(nums):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ", ".join(f"{a}" if a == b else f"{a}–{b}" for a, b in out)


def _blocks_label(blocks) -> str:
    return f"Block{'s' if len(blocks) > 1 else ''} {_ranges(blocks)}"


SMALL_HOLE_DEG2 = 3e-7  # about 3,000 m²: the gaps left where streets cross


def _fill_small_holes(geom):
    from shapely.geometry import MultiPolygon, Polygon

    polys = [geom] if geom.geom_type == "Polygon" else list(geom.geoms)
    kept = [Polygon(p.exterior, [r for r in p.interiors if Polygon(r).area > SMALL_HOLE_DEG2]) for p in polys]
    return kept[0] if len(kept) == 1 else MultiPolygon(kept)


def anchor(sub: str, code: str) -> str:
    """GitHub's heading anchor for '### {sub} ({code})' in the restrictions report."""
    return "#" + re.sub(r"[^\w\- ]", "", f"{sub} ({code})".lower()).replace(" ", "-")


def subdivision_records() -> dict[str, dict]:
    """Subdivision code → name, plats, and what the restrictions survey found for it."""
    manifest = plats.load_manifest()
    out = json.loads(restrictions.RESULTS_PATH.read_text())
    reads = {}
    if restrictread.RESULTS_PATH.exists():
        reads = {r["id"]: r for r in json.loads(restrictread.RESULTS_PATH.read_text())["read"]}
    on_plat = defaultdict(list)
    if platocr.RESULTS_PATH.exists():
        for p in json.loads(platocr.RESULTS_PATH.read_text())["plats"]:
            if {"restrictions", "covenants"} & set(p.get("terms") or []):
                on_plat[p["plat"]].append(p)

    subs: dict[str, dict] = {}
    plat_code = {}
    for name, info in manifest["plats"].items():
        s = subs.setdefault(info["subdivision_code"], {
            "code": info["subdivision_code"], "name": info["subdivision"], "plats": [],
            "declarations": [], "on_plat": [], "indexed": 0})
        s["plats"].append(name)
        plat_code[name] = info["subdivision_code"]

    filed = set()
    for e in out.get("filed_with_plat", []):
        r = reads.get(e["id"], {})
        if r.get("unrelated"):
            continue
        filed.add(e["id"])
        d = _declaration(e, r)
        for code in {plat_code[p] for p in e["plats"] if p in plat_code}:
            subs[code]["declarations"].append(d)
    for r in reads.values():
        # a one-page filing is a lot's certificate or notice, not a declaration
        if (r.get("source") == "candidate" and r["id"] not in filed and not r.get("unrelated")
                and r["kind"] in DECL_KINDS and r["pages"] >= 2 and r.get("blocks")):
            d = _declaration(r, r)
            for code in {plat_code[p] for p in r["plats"] if p in plat_code}:
                subs[code]["declarations"].append(d)
    for p, rows in on_plat.items():
        if p in plat_code:
            for x in rows:
                subs[plat_code[p]]["on_plat"].append({"book_page": x["book_page"], "recorded": x["recorded"],
                                                      "terms": x["terms"]})
    for e in out.get("instruments", []):
        for code in {plat_code[p] for p in e.get("plats") or [] if p in plat_code}:
            subs[code]["indexed"] += 1

    for s in subs.values():
        s["declarations"].sort(key=lambda d: d["recorded"])
        s["status"] = _status(s)
        s["report"] = REPORT_URL + anchor(s["name"], s["code"]) if s["status"] != "none" else REPORT_URL
    return subs


def _declaration(e: dict, r: dict) -> dict:
    """What the map shows of one instrument: e an entry (filed with a plat, or a read candidate), r its reading."""
    racial = "racial restriction" in r.get("features", [])
    return {"recorded": e["recorded"], "book_page": e["book_page"], "document": e.get("docid"),
            "kind": r.get("kind", e.get("doc_type")), "term": r.get("term"),
            "features": [f for f in r.get("features", []) if f != "racial restriction"], "racial": racial,
            "released": restrictions.RACIAL_RELEASES.get(e["book_page"]) if racial else None,
            "expired": restrictions.RACIAL_EXPIRED.get(e["book_page"]) if racial else None,
            "note": r.get("note"), "plats": e["plats"],
            "blocks": sorted(int(b) for b in r["blocks"]) if r.get("blocks") else None}  # None: every block


def _status(s: dict) -> str:
    racial = [d for d in s["declarations"] if d["racial"]]
    if any(not d["released"] and not d["expired"] for d in racial):
        return "racial"
    if any(d["expired"] for d in racial):
        return "expired"
    if racial:
        return "released"
    return "restrictions" if s["declarations"] or s["on_plat"] or s["indexed"] else "none"


def build(refresh: bool = False, echo=print) -> dict:
    import shapely

    parcels = fetch_parcels(refresh, echo)
    subs = subdivision_records()
    county_blocks = defaultdict(set)
    for f in parcels["features"]:
        pid = (f["properties"] or {}).get("GeoPropID") or ""
        if pid.startswith("OP"):
            code, block = code_block(pid)
            if block is not None:
                county_blocks[code].add(block)
    # keep only blocks the county has; a filed declaration left with none covers every block
    for code, s in subs.items():
        kept = []
        for d in s["declarations"]:
            if d["blocks"] is not None:
                named = sorted(set(d["blocks"]) & county_blocks[code])
                if not named and d["document"] is None:
                    continue  # a walk candidate whose blocks are not this subdivision's
                d = dict(d, blocks=named or None)
            kept.append(d)
        s["declarations"] = kept

    def racial_key(code, block):
        ds = subs[code]["declarations"] if code in subs else []
        return frozenset(d["book_page"] for d in ds
                         if d["racial"] and (d["blocks"] is None or block in d["blocks"]))

    parts = {}  # code → {racial key: part number}, numbered from the lowest block
    for code, blocks in county_blocks.items():
        keys = {}
        for b in sorted(blocks):
            keys.setdefault(racial_key(code, b), len(keys))
        parts[code] = keys

    def group(pid):
        code, block = code_block(pid)
        keys = parts.get(code, {})
        return code if len(keys) <= 1 else f"{code}#{keys.get(racial_key(code, block), 0)}"

    shapes = outlines(parcels, group)
    features = []
    for i, (key, geom) in enumerate(sorted(shapes.items())):
        geom = shapely.set_precision(geom, 1e-6)  # about 10 cm
        if geom.is_empty:
            continue
        code = key[:4]
        s = subs.get(code) or {"code": code, "name": f"Subdivision {code}", "plats": [], "declarations": [],
                               "on_plat": [], "indexed": 0, "status": "none", "report": REPORT_URL}
        s = dict(s, note=None)
        blocks = sorted(county_blocks[code])
        if "#" in key:
            n = int(key.split("#")[1])
            blocks = [b for b in blocks if parts[code].get(racial_key(code, b)) == n]
            s["name"] = f"{s['name']}, {_blocks_label(blocks)}"
        here = [d for d in s["declarations"] if d["blocks"] is None or set(d["blocks"]) & set(blocks)]
        s["elsewhere"] = len(s["declarations"]) - len(here)
        s["declarations"] = [dict(d, blocks_label=_blocks_label(d["blocks"]) if d["blocks"] else None) for d in here]
        s["status"] = _status(s)
        if "#" in key and not any(d["racial"] for d in here):
            racial = {b for d in subs[code]["declarations"] if d["racial"] for b in d["blocks"] or []}
            s["note"] = (f"The racial covenants in this subdivision's declarations name Blocks {_ranges(racial)}; "
                         "these blocks are not among them.")
        if code in REPLATS:
            s["note"] = REPLATS[code]["note"]
        for r in REPLATS.values():
            if code == r["code"]:
                s["note"] = r["later_note"]
        props = {k: s[k] for k in ("code", "name", "status", "report", "indexed", "note", "elsewhere")}
        # Mapbox flattens nested properties to strings, so the details travel as JSON
        props["details"] = json.dumps({k: s[k] for k in ("plats", "declarations", "on_plat")})
        features.append({"type": "Feature", "id": i, "properties": props,
                         "geometry": json.loads(shapely.to_geojson(geom))})
    fc = {"type": "FeatureCollection", "features": features}
    MAP_DIR.mkdir(exist_ok=True)
    GEOJSON_PATH.write_text(json.dumps(fc, separators=(",", ":")))
    carried_overlay(parcels, subs)
    return fc

# Document images for the map's links. They name the parties, so they are gitignored; the local
# server links every downloaded image, a deploy bundle only those the map refers to.
PAGE_CACHE = plats.DEEDS_DIR / "cache" / "pages" / "map-avif"
BUNDLE_DIR = PROJECT_ROOT / "deploy" / "bundle"
MAP_FILES = ["index.html", "subdivisions.geojson", "preplat.geojson", "carried.geojson", "highlights.json"]


def map_refs() -> set[str]:
    """The book and page of every document the built map refers to."""
    refs = set()
    for f in json.loads(GEOJSON_PATH.read_text())["features"]:
        d = json.loads(f["properties"]["details"])
        for x in d["declarations"]:
            refs.add(x["book_page"])
            if x.get("released"):
                refs.add(x["released"]["book_page"])
        refs |= {x["book_page"] for x in d["on_plat"]}
    for name in ("preplat.geojson", "carried.geojson"):
        for f in json.loads((MAP_DIR / name).read_text())["features"]:
            props = f["properties"]
            refs.add(props.get("book_page") or props.get("declaration"))
    return {r for r in refs if r}


def doc_images() -> dict[str, dict]:
    from pvcouncil import mapserve

    return mapserve.index_docs(sorted(plats.IMAGES_DIR.rglob("*.pdf")))


def bundle(token: str, out=BUNDLE_DIR, workers: int = 6, echo=print) -> dict:
    """The site to deploy, in out/site/ laid out as it is served: the map, its config.js (with the
    Mapbox token), and under docs/ every page of the documents the map refers to as AVIF at both
    sizes, with docs/index.json. The CDK stack uploads site/ to S3; mapserve.py and the Dockerfile
    are copied alongside to run the same files in a container. Pages are rendered into the local
    page cache first, so a rebuild reuses them. Returns the counts of documents and pages and the
    references with no image."""
    import shutil
    from concurrent.futures import ProcessPoolExecutor

    from pvcouncil import mapserve

    if out.exists():
        shutil.rmtree(out)
    site = out / "site"
    (site / "docs").mkdir(parents=True)
    for name in MAP_FILES:
        shutil.copy2(MAP_DIR / name, site / name)
    (site / "config.js").write_text(f"window.MAPBOX_TOKEN = {json.dumps(token)};\n")
    images = doc_images()
    refs = sorted(map_refs())
    missing = [r for r in refs if plats._slug(r) not in images]
    docs = {plats._slug(r): images[plats._slug(r)] for r in refs if r not in missing}
    jobs = [(d["path"], PAGE_CACHE, s, n, size) for s, d in docs.items()
            for n in range(1, len(d["sizes"]) + 1) for size in mapserve.SIDES]
    todo = [j for j in jobs if not mapserve.page_path(*j[1:]).exists()]
    echo(f"{len(docs)} documents, {len(jobs) // len(mapserve.SIDES)} pages; rendering {len(todo)} page images")
    with ProcessPoolExecutor(workers) as pool:
        for i, _ in enumerate(pool.map(mapserve.write_page, *zip(*todo)) if todo else [], 1):
            if i % 200 == 0:
                echo(f"  {i}/{len(todo)}")
    for _, cache, s, n, size in jobs:
        dest = mapserve.page_path(site / "docs", s, n, size)
        dest.parent.mkdir(exist_ok=True)
        shutil.copy2(mapserve.page_path(cache, s, n, size), dest)
    (site / "docs" / "index.json").write_text(json.dumps({s: d["sizes"] for s, d in docs.items()}, separators=(",", ":")))
    shutil.copy2(Path(__file__).with_name("mapserve.py"), out / "mapserve.py")
    shutil.copy2(out.parent / "Dockerfile", out / "Dockerfile")
    return {"docs": len(docs), "pages": len(jobs) // len(mapserve.SIDES), "missing": missing}
