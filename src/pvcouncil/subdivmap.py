"""A map of Prairie Village's subdivisions and their recorded restrictions.

Subdivision outlines are built from the county's public Prairie Village parcel layer
(maps.jocogov.org, PrairieVillage/Parcels): a platted parcel's ID reads OP<subdivision
code><block> <lot>, so the parcels of each subdivision code are merged into one shape,
closing the streets between blocks. Only parcel IDs and geometry are fetched; the
layer's owner fields are not. The raw parcels are cached in deeds/cache/gis/.

Each subdivision carries what the restrictions survey found: declarations filed with
its plats (with their terms, and whether a racial covenant was released), restrictions
written on the plat, and how many indexed restriction instruments name it. The result,
map/subdivisions.geojson, is drawn by map/index.html.
"""

import json
import re
import urllib.parse
import urllib.request
from collections import defaultdict

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
# Subdivisions whose declarations each cover only some of the county code's blocks: each part is
# drawn as its own shape. A part without "declarations" keeps the declarations filed with the
# plat; one with a list carries just those (read walk candidates, by book and page). The last
# part, blocks None, takes the remaining blocks.
SPLITS = {"5500": [
    # Prairie Village's 1941 declaration (MISC 26 349), released in 2007, names Lots in Blocks 1-5 only
    {"blocks": {1, 2, 3, 4, 5}, "label": "Blocks 1–5"},
    {"blocks": {36}, "label": "Block 36", "declarations": ["MISC 43 573"]},
    {"blocks": None, "label": "Blocks 6–35 and 37–56", "declarations": [],
     "note": "The 1941 declaration (MISC 26 349), whose racial covenant was released in 2007, covers "
             "Blocks 1–5 only, and Block 36 has its own 1950 declaration. The survey has not yet matched "
             "a declaration to these blocks."},
]}
# Lots re-platted into a later subdivision: a replat does not release the original
# declaration, so they are drawn with the subdivision they were first platted in.
REPLATS = {
    # Prairie Fields (PLAT 17 46, 1954) re-platted Lots 1-5, Block 1 of Meadowbrook Downs as its
    # Block 22, Lots 1-5; MISC 43 168 (1949) covers all six lots and renews every ten years
    "2350": {"code": "4200", "block": 22, "lots": {1, 2, 3, 4, 5},
             "note": "Lots 1–5 were re-platted in 1954 as Prairie Fields Block 22, Lots 1–5 (PLAT 17 46). "
                     "The replat did not release the 1949 declaration, which renews every ten years.",
             "later_note": "Block 22, Lots 1–5 are shown with Meadowbrook Downs, the 1949 subdivision "
                           "they were re-platted from, whose declaration still covers them."},
}


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


def outlines(parcels: dict) -> dict:
    """Subdivision code → merged (Multi)Polygon of its platted parcels."""
    from shapely.geometry import shape
    from shapely.ops import unary_union

    by_code = defaultdict(list)
    for f in parcels["features"]:
        pid = (f["properties"] or {}).get("GeoPropID") or ""
        if pid.startswith("OP") and f.get("geometry"):
            by_code[_group(pid)].append(shape(f["geometry"]).buffer(0))
    out = {}
    for code, geoms in by_code.items():
        merged = unary_union([g.buffer(CLOSE_DEG, join_style="mitre") for g in geoms])
        out[code] = _fill_small_holes(merged.buffer(-CLOSE_DEG, join_style="mitre")).simplify(SIMPLIFY_DEG)
    return out


def _group(pid: str) -> str:
    """The shape a parcel ID (OP<code><block> <lot>) is drawn in: its code, a REPLATS original, or code+."""
    code = pid[2:6]
    block = int(pid[6:10]) if pid[6:10].isdigit() else None  # a few blocks are lettered, e.g. 00BS
    lot = re.match(r"\d*", pid[11:]).group()
    for orig, r in REPLATS.items():
        if code == r["code"] and block == r["block"] and lot and int(lot) in r["lots"]:
            return orig
    for i, part in enumerate(SPLITS.get(code, [])):
        if part["blocks"] is None or block in part["blocks"]:
            return f"{code}#{i}"
    return code


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

    for e in out.get("filed_with_plat", []):
        r = reads.get(e["id"], {})
        if r.get("unrelated"):
            continue
        d = _declaration(e, r)
        for code in {plat_code[p] for p in e["plats"] if p in plat_code}:
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
            "note": r.get("note"), "plats": e["plats"]}


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

    shapes = outlines(fetch_parcels(refresh, echo))
    subs = subdivision_records()
    by_book_page = {}
    if restrictread.RESULTS_PATH.exists():
        by_book_page = {r["book_page"]: r for r in json.loads(restrictread.RESULTS_PATH.read_text())["read"]}
    features = []
    for i, (key, geom) in enumerate(sorted(shapes.items())):
        geom = shapely.set_precision(geom, 1e-6)  # about 10 cm
        if geom.is_empty:
            continue
        code = key[:4]
        s = subs.get(code) or {"code": code, "name": f"Subdivision {code}", "plats": [], "declarations": [],
                               "on_plat": [], "indexed": 0, "status": "none", "report": REPORT_URL}
        s = dict(s, note=None)
        if code in REPLATS:
            s["note"] = REPLATS[code]["note"]
        for r in REPLATS.values():
            if code == r["code"]:
                s["note"] = r["later_note"]
        if "#" in key:
            part = SPLITS[code][int(key.split("#")[1])]
            s["name"] = f"{s['name']}, {part['label']}"
            if "declarations" in part:
                s["declarations"] = [_declaration(by_book_page[bp], by_book_page[bp])
                                     for bp in part["declarations"] if bp in by_book_page]
                s["note"] = part.get("note")
                s["status"] = _status(s)
        props = {k: s[k] for k in ("code", "name", "status", "report", "indexed", "note")}
        # Mapbox flattens nested properties to strings, so the details travel as JSON
        props["details"] = json.dumps({k: s[k] for k in ("plats", "declarations", "on_plat")})
        features.append({"type": "Feature", "id": i, "properties": props,
                         "geometry": json.loads(shapely.to_geojson(geom))})
    fc = {"type": "FeatureCollection", "features": features}
    MAP_DIR.mkdir(exist_ok=True)
    GEOJSON_PATH.write_text(json.dumps(fc, separators=(",", ":")))
    return fc
