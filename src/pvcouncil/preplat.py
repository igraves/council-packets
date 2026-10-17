"""Covenants put on land before it was platted.

J. C. Nichols often had owners restrict their acreage before he bought and platted it, and
restricted land himself before filing a plat. Such instruments are indexed to the bare land
(Sec/Twp/Rng and quarter), not to any subdivision, so the plat-based survey misses them.

Each Public Land Survey section that Prairie Village's subdivisions occupy (BLM's national
PLSS layer, overlaid on map/subdivisions.geojson) gets its earliest plat date. The
"Miscellaneous" index is pulled month by month for the years before it (cached with the
survey's walk months), and every instrument indexed to bare land in that section before that
date is read. deeds/preplat.json carries the classification only; images and OCR text stay
local like the other restriction instruments.
"""

import calendar
import json
import re
import urllib.parse
import urllib.request
from collections import defaultdict

from pvcouncil import plats, restrictions, restrictread, subdivmap
from pvcouncil.recorder import Recorder

PLSS_URL = "https://gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer/2/query"
TOWNSHIPS = ("KS060120S0250E0", "KS060130S0250E0")  # 6th P.M., T12S and T13S, R25E
SECTIONS_PATH = plats.DEEDS_DIR / "cache" / "gis" / "plss-sections.geojson"
RESULTS_PATH = plats.DEEDS_DIR / "preplat.json"
OVERLAY_PATH = subdivmap.MAP_DIR / "preplat.geojson"
YEARS_BEFORE = 10
_LEGAL = re.compile(r"Sec:(\d+)\s+Twshp:(\d+)\s+Range:(\d+)(?:\s+Qtr:(\w+))?(?:\s+QtrQtr:(\w+))?")
_NICHOLS = re.compile(r"NICHOLS", re.I)


def sections(refresh: bool = False) -> dict[tuple[int, int, int], object]:
    """(section, township, range) → shapely polygon, for T12S and T13S R25E."""
    from shapely.geometry import shape

    if refresh or not SECTIONS_PATH.exists():
        ids = ",".join(f"'{t}'" for t in TOWNSHIPS)
        q = urllib.parse.urlencode({"where": f"PLSSID IN ({ids})", "outFields": "PLSSID,FRSTDIVNO",
                                    "outSR": 4326, "f": "geojson"})
        req = urllib.request.Request(f"{PLSS_URL}?{q}", headers={"User-Agent": "Mozilla/5.0 (council-packets)"})
        with urllib.request.urlopen(req, timeout=120) as r:
            SECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SECTIONS_PATH.write_bytes(r.read())
    out = {}
    for f in json.loads(SECTIONS_PATH.read_text())["features"]:
        p = f["properties"]
        # PLSSID: state, meridian, township (3) + fraction + direction, range (3) + fraction + direction
        out[(int(p["FRSTDIVNO"]), int(p["PLSSID"][4:7]), int(p["PLSSID"][9:12]))] = shape(f["geometry"])
    return out


def part(geom, path: str):
    """An aliquot part of a section by its legal path, largest division first: "SE/W/W/N" is the north
    half of the west half of the west half of the southeast quarter. Sections are close enough to
    square for halving their bounds."""
    from shapely.geometry import box

    for step in path.split("/") if path else []:
        x0, y0, x1, y1 = geom.bounds
        xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
        b = {"NW": (x0, ym, xm, y1), "NE": (xm, ym, x1, y1), "SW": (x0, y0, xm, ym), "SE": (xm, y0, x1, ym),
             "N": (x0, ym, x1, y1), "S": (x0, y0, x1, ym), "E": (xm, y0, x1, y1), "W": (x0, y0, xm, y1)}[step]
        geom = geom.intersection(box(*b))
    return geom


QUARTERS = ("NW", "NE", "SW", "SE")


def first_plats() -> dict[tuple[int, int, int, str, str], dict]:
    """Each quarter-quarter section (40 acres) our subdivisions occupy → its earliest plat (date and
    name) among them. Not whole sections: a section was often platted piece by piece over decades."""
    from shapely.geometry import shape

    manifest = plats.load_manifest()
    first = {}
    for e in manifest["instruments"]:
        if e["match"] in ("exact", "variant"):
            for p in e.get("plats") or [e["plat"]]:
                if p not in first or e["recorded"] < first[p][0]:
                    first[p] = (e["recorded"], p)
    by_code = {}
    for name, info in manifest["plats"].items():
        if name in first and (info["subdivision_code"] not in by_code or first[name] < by_code[info["subdivision_code"]]):
            by_code[info["subdivision_code"]] = first[name]
    subs = json.loads(subdivmap.GEOJSON_PATH.read_text())["features"]
    out = {}
    shapes = [(by_code[s["properties"]["code"]], shape(s["geometry"])) for s in subs if s["properties"]["code"] in by_code]
    for key, sec in sections().items():
        for q in QUARTERS:
            for qq in QUARTERS:
                tract = part(sec, f"{q}/{qq}")
                dates = [d for d, g in shapes if g.intersection(tract).area > 0.01 * tract.area]
                if dates:
                    d, p = min(dates)
                    out[(*key, q, qq)] = {"first_plat": d, "plat": p}
    return out


def months(firsts: dict) -> list[tuple[int, int]]:
    """Months in the YEARS_BEFORE years up to each section's first plat."""
    out = set()
    for v in firsts.values():
        n = int(v["first_plat"][:4]) * 12 + int(v["first_plat"][5:7]) - 1
        out |= {divmod(n - k, 12) for k in range(YEARS_BEFORE * 12 + 1)}
    return sorted((y, m + 1) for y, m in out)


def fetch(echo=print) -> None:
    """Pull every Miscellaneous instrument for the pre-plat months not yet cached."""
    todo = [(y, m) for y, m in months(first_plats()) if not restrictions._month_file(y, m).exists()]
    echo(f"{len(todo)} months to fetch")
    if not todo:
        return
    rec = Recorder().login()
    for i, (y, m) in enumerate(todo, 1):
        last = calendar.monthrange(y, m)[1]
        rows = rec.search(doc_types=[restrictions.MISC_DOC_TYPE], recorded=(f"{m:02d}/01/{y}", f"{m:02d}/{last}/{y}"))
        restrictions._month_file(y, m).write_text(json.dumps({"year": y, "month": m, "rows": rows}))
        if i % 10 == 0 or i == len(todo):
            echo(f"[{i}/{len(todo)}] {y}-{m:02d}: {len({r['instrumentReference_id'] for r in rows})} instruments")


def find(firsts: dict) -> list[dict]:
    """Cached Miscellaneous instruments indexed to bare land in one of our sections before its first
    plat. Party names are not kept, only whether a Nichols company or an UNKNOWN/PUBLIC party is one."""
    groups = defaultdict(list)
    for f in sorted((restrictions.CACHE_DIR / "misc").glob("misc-*.json")):
        for r in json.loads(f.read_text())["rows"]:
            groups[str(r["instrumentReference_id"])].append(r)
    out = []
    for iid, rows in groups.items():
        names = {(r["LastName"] or "").strip() for r in rows} | {(r["RelatedName"] or "").strip() for r in rows}
        if any(restrictions._UTILITY.search(n) for n in names):
            continue
        rec = rows[0]["RecordedDate"][:10]
        found = {}
        for r in rows:
            for m in _LEGAL.finditer(r["PropertyLegalShort"] or ""):
                sec = tuple(int(x) for x in m.groups()[:3])
                # a legal without its quarter (or quarter-quarter) could be any of them: it counts if
                # it predates the first plat on one
                for q in [m.group(4)] if m.group(4) in QUARTERS else QUARTERS:
                    for qq in [m.group(5)] if m.group(5) in QUARTERS else QUARTERS:
                        f = firsts.get((*sec, q, qq))
                        if f and rec < f["first_plat"]:
                            key = (sec, m.group(4), m.group(5))
                            if key not in found or f["first_plat"] < found[key]["first_plat"]:
                                found[key] = f
        if not found:
            continue
        legals = [{"section": list(k[0]), "qtr": k[1], "qtrqtr": k[2]} for k in sorted(found, key=str)]
        first = min(found.values(), key=lambda f: f["first_plat"])
        out.append({"id": iid, "book_page": rows[0]["ReferenceDisplayValue"], "recorded": rec,
                    "doc_type": rows[0]["Value"], "legals": legals,
                    "first_plat": first["first_plat"], "later_plat": first["plat"],
                    "nichols": any(_NICHOLS.search(n) for n in names),
                    "unknown_party": any(n.upper() in ("UNKNOWN", "PUBLIC", "THE PUBLIC") for n in names)})
    return sorted(out, key=lambda e: e["recorded"])


def read(entries: list[dict], workers: int = 4, echo=print, offline: bool = False) -> list[dict]:
    """Download, OCR and classify each instrument (images and text stay local)."""
    if not offline:
        restrictread.download(entries, echo=echo)
    restrictread.ocr_all(entries, workers=workers, echo=echo)
    for e in entries:
        txt = restrictread.OCR_DIR / f"{restrictread.image_path(e).stem}.txt"
        if txt.exists():
            c = restrictread.classify(txt.read_text())
            e.update({k: c[k] for k in ("kind", "features", "term", "pages", "words")})
            e["racial"] = "racial restriction" in c["features"]
    return entries


# Racial covenants on acreage, read by eye, with the land they cover. "parts" are aliquot parts
# (section, path for part()); "strip" is land within half_ft of the line between two sections,
# from_ft to to_ft south of their north line. Small exceptions in the descriptions are not cut out.
COVENANTS = {
    "MISC 15 305": {
        "recorded": "1927-09-06", "parts": [(22, "SW/E/E/N"), (22, "SE/W/N")], "ended": "1952", "term": "25 years",
        "by": "neighboring owners", "nichols": False,
        "summary": "Owners of adjoining tracts restricted the north half of their land together, barring "
                   "ownership or occupancy by Black people.",
    },
    "MISC 16 521": {
        "recorded": "1929-06-18", "parts": [(27, "SW/N")], "ended": "1954", "term": "25 years", "by": "the owners", "nichols": False,
        "summary": "Restriction Agreement made as part of an option sale of the land to the north (MISC 16 490): "
                   "the sellers restricted the land they kept, barring ownership or occupancy by Black people.",
    },
    "MISC 14 315": {
        "recorded": "1930-02-25", "strip": {"between": (15, 16), "half_ft": 325, "from_ft": 200, "to_ft": 2431.28},
        "ended": "1955", "term": "25 years", "by": "owners along Porter Road and the J. C. Nichols Company",
        "nichols": True,
        "summary": "Owners along Porter Road (Mission Road) restricted land within 325 feet of it, while the "
                   "J. C. Nichols Company held an option to buy part of it, barring ownership or occupancy "
                   "by Black people.",
    },
    "MISC 43 532": {
        "recorded": "1950-02-03", "parts": [(22, "NW/E")], "ended": "1990", "term": "40 years", "by": "the J. C. Nichols Company",
        "nichols": True,
        "summary": "A declaration whose only restriction is racial, the same form as Prairie Village Block 36's, "
                   "filed four months before the Prairie Hills plat.",
    },
}
FT_LAT = 1 / 364_000  # degrees of latitude per foot, near 39° N


def covenant_area(c: dict, secs: dict, twp: int = 12, rng: int = 25):
    from math import cos, radians

    from shapely.geometry import box
    from shapely.ops import unary_union

    if "parts" in c:
        return unary_union([part(secs[(s, twp, rng)], p) for s, p in c["parts"]])
    s = c["strip"]
    a, b = secs[(s["between"][0], twp, rng)], secs[(s["between"][1], twp, rng)]
    line_x = (a.bounds[0] + b.bounds[2]) / 2  # their shared line, east of b and west of a
    north = max(a.bounds[3], b.bounds[3])
    ft_lon = FT_LAT / cos(radians(north))
    return box(line_x - s["half_ft"] * ft_lon, north - s["to_ft"] * FT_LAT,
               line_x + s["half_ft"] * ft_lon, north - s["from_ft"] * FT_LAT)


def overlay(entries: list[dict]) -> dict:
    """map/preplat.geojson: the COVENANTS' land, with what the map card shows."""
    import shapely
    from shapely.geometry import shape

    secs = sections()
    by_bp = {e["book_page"]: e for e in entries}
    subs = json.loads(subdivmap.GEOJSON_PATH.read_text())["features"]
    feats = []
    for i, (bp, c) in enumerate(sorted(COVENANTS.items(), key=lambda kv: kv[1]["recorded"])):
        e = by_bp.get(bp, {})
        area = covenant_area(c, secs)
        now = sorted({s["properties"]["name"] for s in subs
                      if shape(s["geometry"]).intersection(area).area > 0.05 * area.area})
        props = {"book_page": bp, "recorded": c["recorded"], "ended": c["ended"], "term": c["term"],
                 "by": c["by"], "nichols": c["nichols"], "summary": c["summary"], "now": json.dumps(now),
                 "later_plat": e.get("later_plat"), "first_plat": e.get("first_plat")}
        feats.append({"type": "Feature", "id": i, "properties": props,
                      "geometry": json.loads(shapely.to_geojson(area.simplify(1e-6)))})
    fc = {"type": "FeatureCollection", "features": feats}
    OVERLAY_PATH.write_text(json.dumps(fc, separators=(",", ":")))
    return fc


def build(entries: list[dict]) -> dict:
    out = {"years_before": YEARS_BEFORE, "sections": len({tuple(x["section"]) for e in entries for x in e["legals"]}),
           "instruments": entries}
    RESULTS_PATH.write_text(json.dumps(out, indent=1) + "\n")
    return out
