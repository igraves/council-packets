"""Prairie Village's parcels as a layer of the subdivision map.

The county's public Prairie Village parcel layer (subdivmap.PARCELS_URL) has a record for each
parcel polygon. This fetches its address, plat, building and land-use fields, joins them to the
parcel outlines subdivmap already caches, and writes map/parcels.geojson, which the map draws once
zoomed in, with each parcel's details above its subdivision's card.

The layer's owner, mailing-address and billing-address fields are never requested. Appraised and
assessed values are fetched with the rest but kept apart, in the gitignored
deeds/cache/gis/pv-parcel-values.json, and are not put on the map.
"""

import json
import re
import urllib.parse
import urllib.request

from pvcouncil import subdivmap

DETAILS_PATH = subdivmap.CACHE_PATH.with_name("pv-parcel-details.json")
VALUES_PATH = subdivmap.CACHE_PATH.with_name("pv-parcel-values.json")
GEOJSON_PATH = subdivmap.MAP_DIR / "parcels.geojson"

# map property -> layer field
DETAILS = {
    "id": "TaxPropID",
    "address": "SALine1",
    "block": "GeoBlock",
    "lot": "GeoLot",
    "legal": "LegalDesc",
    "acres": "AreaCAcre",
    "year": "YearBuilt",
    "style": "ResStylDsc",
    "units": "DwellUnits",
    "grade": "GradeDesc",
    "cdu": "CDUDesc",
    "condition": "CondDesc",
    "zoning": "Zoning",
    "class": "GenClass",
    "use": "LBCSFunDsc",
    "structure": "LBCSStrDsc",
    "ward": "CityWardName",
}
VALUES = {
    "land_value": "LandValue",
    "improvement_value": "ImprValue",
    "total_value": "TotalValue",
    "assessed_land": "AssLandVal",
    "assessed_improvement": "AssImprVal",
    "assessed_total": "AssTotVal",
    "assessment_pct_land": "AssLandPct",
    "assessment_pct_improvement": "AssImprPct",
}


def fetch(refresh: bool = False, echo=print) -> dict:
    """GeoPropID -> the parcel's DETAILS fields. The VALUES fields are saved to VALUES_PATH, keyed by
    GeoPropID, and left out of what this returns."""
    if DETAILS_PATH.exists() and not refresh:
        return json.loads(DETAILS_PATH.read_text())
    details, values, offset = {}, {}, 0
    fields = ["GeoPropID", *DETAILS.values(), *VALUES.values()]
    while True:
        q = urllib.parse.urlencode({
            "where": "1=1", "outFields": ",".join(fields), "returnGeometry": "false",
            "orderByFields": "OBJECTID", "resultOffset": offset, "resultRecordCount": subdivmap.PAGE, "f": "json"})
        req = urllib.request.Request(f"{subdivmap.PARCELS_URL}?{q}", headers={"User-Agent": "Mozilla/5.0 (council-packets)"})
        with urllib.request.urlopen(req, timeout=120) as r:
            page = json.load(r)
        if "error" in page:
            raise RuntimeError(f"parcel layer: {page['error']}")
        for f in page["features"]:
            a = f["attributes"]
            details.setdefault(a["GeoPropID"], {k: a[src] for k, src in DETAILS.items()})
            values.setdefault(a["GeoPropID"], {k: a[src] for k, src in VALUES.items()})
        offset += len(page["features"])
        echo(f"{offset} parcel records")
        if len(page["features"]) < subdivmap.PAGE:
            break
    DETAILS_PATH.parent.mkdir(parents=True, exist_ok=True)
    DETAILS_PATH.write_text(json.dumps(details))
    VALUES_PATH.write_text(json.dumps(values))
    return details


def _clean(d: dict) -> dict:
    """The map's properties for one parcel: whitespace trimmed, plat numbers without leading zeros,
    the ward as its number, and empty or zero fields dropped."""
    out = {}
    for k, v in d.items():
        if isinstance(v, str):
            v = re.sub(r"\s+", " ", v).strip()
            if k == "address" and v.startswith("0 "):
                v = ""  # "0 NS NT": no street address
            elif k in ("block", "lot"):
                v = v.lstrip("0")
            elif k == "ward":
                v = v.rsplit(" ", 1)[-1]
        if v not in (None, "", 0):
            out[k] = v
    return out


# simplified by about 30 cm, which a parcel's outline doesn't show at any zoom, and rounded to about
# 10 cm: a third fewer points, so the layer loads faster
SIMPLIFY_DEG = 0.000003


def _round(coords, nd=6):
    """Coordinates to nd decimals (6 is about 10 cm)."""
    if isinstance(coords[0], (int, float)):
        return [round(c, nd) for c in coords]
    return [_round(c, nd) for c in coords]


def build(refresh: bool = False, echo=print) -> dict:
    """Write map/parcels.geojson: every parcel outline with its details."""
    from shapely.geometry import mapping, shape

    details = fetch(refresh, echo)
    parcels = subdivmap.fetch_parcels(refresh, echo)
    features = []
    for f in parcels["features"]:
        pid = f["properties"]["GeoPropID"]
        g = f["geometry"]
        if not g or pid not in details:
            continue
        g = mapping(shape(g).simplify(SIMPLIFY_DEG, preserve_topology=True))
        features.append({"type": "Feature", "properties": _clean(details[pid]),
                         "geometry": {"type": g["type"], "coordinates": _round(g["coordinates"])}})
    fc = {"type": "FeatureCollection", "features": features}
    GEOJSON_PATH.write_text(json.dumps(fc, separators=(",", ":")))
    return fc
