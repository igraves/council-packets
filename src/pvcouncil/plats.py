"""Survey of recorded plats for every Prairie Village subdivision.

The Recorder of Deeds indexes a plat under the plat's own name as the party
name ("KENILWORTH", "PRAIRIE VILLAGE"), and older plats carry no legal
description at all, so plats are found by name search, not by subdivision.

For every plat name in parcels/<year>.csv this runs one prefix name search
limited to the PLAT category, caches the raw rows under deeds/cache/plats/
(gitignored: rows can carry other party names), and builds:

  deeds/plats.json     one entry per recorded plat instrument, matched to
                       our plat names; no party names beyond the plat name
  subdivision-plats.md the report: original platting date and every plat
                       recorded for each subdivision

A prefix search also finds plats elsewhere in the county that share a name
("EVERGREEN", "WEST HILL"), so each instrument is marked "exact" when its
index name is one of our plat names and "prefix" when it only starts with one.
Prefix matches whose section/township/range falls in Township 12 or 13,
Range 25 (where Prairie Village lies) are treated as name variants of our
plats, such as "MISSION VILLAGE BLOCK 2"; the rest are listed as elsewhere.
"""

import json
import re
from collections import defaultdict
from pathlib import Path

from pvcouncil import parcels as pm
from pvcouncil.config import PROJECT_ROOT
from pvcouncil.recorder import PLAT_CATEGORY, PLAT_DOC_TYPES, Recorder

DEEDS_DIR = PROJECT_ROOT / "deeds"
CACHE_DIR = DEEDS_DIR / "cache" / "plats"
MANIFEST_PATH = DEEDS_DIR / "plats.json"
MAX_ROWS = 2000  # per name search; no plat name should come near this
PV_TOWNSHIPS = {(12, 25), (13, 25)}  # (township, range) pairs covering Prairie Village

_ORDINALS = {"FIRST": "1ST", "SECOND": "2ND", "THIRD": "3RD", "FOURTH": "4TH", "FIFTH": "5TH",
             "SIXTH": "6TH", "SEVENTH": "7TH", "EIGHTH": "8TH", "NINTH": "9TH", "TENTH": "10TH"}


def norm(name: str) -> str:
    """Normalize a plat name for comparison: 'Prairie Ridge Second Plat' == 'PRAIRIE RIDGE 2ND PLAT'."""
    s = name.upper().replace("&", " AND ")
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    words = [_ORDINALS.get(w, w) for w in s.split()]
    words = [w for w in words if w not in ("THE", "NO")]
    return " ".join(words)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def plat_names(year: int | None = None) -> dict[str, dict]:
    """Our plat names, each with its subdivision code, subdivision name and parcel count."""
    out: dict[str, dict] = {}
    for p in pm.load(year):
        if not p["plat"]:
            continue
        e = out.setdefault(p["plat"], {"plat": p["plat"], "subdivision_code": p["subdivision_code"],
                                       "subdivision": p["subdivision"], "parcels": 0})
        e["parcels"] += 1
    return dict(sorted(out.items()))


def search_term(plat: str) -> str:
    """The name to search for: index names drop 'REPLAT OF ...'-style prefixes."""
    m = re.match(r"(?:CORRECTED PLAT|REPLAT|RESURVEY)\s+OF\s+(?:PART OF\s+)?(?:LOTS?\s+[\d\s,AND-]+)?(.*)", plat)
    return (m.group(1).strip() if m and m.group(1).strip() else plat)


def fallback_terms(term: str) -> list[str]:
    """Looser names to try when a term finds nothing: no punctuation, then shorter."""
    out = []
    plain = re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9& ]", " ", term.upper())).strip()
    out.append(plain)
    short = re.sub(r"\s+(?:NO\s*\d+|\d+(?:ST|ND|RD|TH)\s+PLAT|EAST|WEST|NORTH|SOUTH)(?:\s+.*)?$", "", plain)
    out.append(short)
    words = plain.split()
    if len(words) > 2:
        out.append(" ".join(words[:2]))
    seen, uniq = {term}, []
    for t in out:
        if t and t not in seen and len(t) >= 6:
            seen.add(t)
            uniq.append(t)
    return uniq


def fetch(names: dict[str, dict], refresh: bool = False, echo=print) -> None:
    """Run (or resume) the name searches, caching raw rows per search term."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    terms = sorted({search_term(n) for n in names})
    def needs(t):
        f = CACHE_DIR / f"{_slug(t)}.json"
        if refresh or not f.exists():
            return True
        c = json.loads(f.read_text())
        return not c["rows"] and "searched" not in c  # zero-result search from before fallbacks
    todo = [t for t in terms if needs(t)]
    echo(f"{len(terms)} search terms, {len(terms) - len(todo)} cached, {len(todo)} to fetch")
    if not todo:
        return
    rec = Recorder().login()
    for i, term in enumerate(todo, 1):
        searched = term
        for t in [term] + fallback_terms(term):
            rows = rec.search(categories=[PLAT_CATEGORY], doc_types=PLAT_DOC_TYPES, name=t,
                              name_match="Like", max_rows=MAX_ROWS)
            searched = t
            if rows:
                break
        (CACHE_DIR / f"{_slug(term)}.json").write_text(
            json.dumps({"term": term, "searched": searched, "rows": rows}))
        n_inst = len({r["instrumentReference_id"] for r in rows})
        capped = " (capped)" if len(rows) >= MAX_ROWS else ""
        via = f" (as {searched!r})" if searched != term else ""
        echo(f"[{i}/{len(todo)}] {term}: {n_inst} instruments{via}{capped}")


def _docid(remarks: str) -> str | None:
    m = re.search(r"DOCID:\s*(\d+)", remarks or "")
    return m.group(1) if m else None


def location(legal: str | None) -> tuple[int, int, int] | None:
    """(section, township, range) from an index legal description, if it has one."""
    m = re.search(r"Sec:\s*(\d+)\s+(?:Twshp|Twp):\s*(\d+)\s+(?:Range|Rng):\s*(\d+)", legal or "")
    return tuple(int(x) for x in m.groups()) if m else None


def build(names: dict[str, dict]) -> dict:
    """Match cached rows to our plat names and write deeds/plats.json."""
    by_norm = {norm(n): n for n in names}
    instruments: dict[str, dict] = {}
    for f in sorted(CACHE_DIR.glob("*.json")):
        cached = json.loads(f.read_text())
        term = norm(cached.get("searched") or cached["term"])
        for r in cached["rows"]:
            # keep only party names that are plat names (they start with the search term);
            # any other party on a plat row is a person or company and stays in the cache
            idx_name = r["LastName"].strip()
            if r["FirstName"].strip() or not norm(idx_name).startswith(term):
                continue
            iid = str(r["instrumentReference_id"])
            e = instruments.setdefault(iid, {
                "id": iid,
                "recorded": r["RecordedDate"],
                "doc_type": r["Value"],
                "book_page": r["ReferenceDisplayValue"],
                "instrument_number": r["InstrumentNumber"],
                "docid": _docid(r["Remarks"]),
                "legal": (r["PropertyLegalShort"] or "").strip(" *") or None,
                "index_names": [],
            })
            if idx_name not in e["index_names"]:
                e["index_names"].append(idx_name)

    for e in instruments.values():
        matches = [by_norm[norm(n)] for n in e["index_names"] if norm(n) in by_norm]
        if matches:
            e["plat"], e["match"] = matches[0], "exact"
        else:
            # the plat whose name (or looser fallback form) matches the most of the index name
            best, best_len = None, 0
            for p in sorted(names):
                for t in [search_term(p)] + fallback_terms(search_term(p)):
                    if len(norm(t)) > best_len and any(norm(n).startswith(norm(t)) for n in e["index_names"]):
                        best, best_len = p, len(norm(t))
            e["plat"] = best
            loc = location(e["legal"])
            if loc is None:
                e["match"] = "prefix"
            else:
                e["match"] = "variant" if (loc[1], loc[2]) in PV_TOWNSHIPS else "elsewhere"
        e["subdivision_code"] = names[e["plat"]]["subdivision_code"] if e["plat"] else None

    manifest = {
        "plats": names,
        "instruments": sorted(instruments.values(), key=lambda e: (e["recorded"], e["book_page"])),
    }
    DEEDS_DIR.mkdir(exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=1) + "\n")
    return manifest


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError("No deeds/plats.json yet. Run `pvc plat-survey`.")
    return json.loads(MANIFEST_PATH.read_text())


def render_markdown(manifest: dict) -> str:
    names = manifest["plats"]
    insts_all = manifest["instruments"]
    by_match = defaultdict(list)
    for e in insts_all:
        by_match[e["match"]].append(e)
    # a subdivision's plats: exact name matches plus name variants located in Prairie Village
    by_plat = defaultdict(list)
    for e in by_match["exact"] + by_match["variant"]:
        if e["plat"]:
            by_plat[e["plat"]].append(e)
    subs = defaultdict(list)
    for n, info in names.items():
        subs[(info["subdivision_code"], info["subdivision"])].append(n)

    found = sum(1 for n in names if by_plat[n])
    lines = [
        "# Prairie Village subdivision plats",
        "",
        "Recorded plats for each subdivision in the 2026 parcel data, from the Johnson County "
        "Recorder of Deeds index. Generated by `pvc plat-survey`; the instrument list with "
        "record IDs is in [deeds/plats.json](deeds/plats.json).",
        "",
        f"- **{len(names)}** plat names in the parcel data, in **{len(subs)}** subdivisions",
        f"- **{found}** plat names matched to at least one recorded plat",
        f"- **{len(by_match['exact'])}** plat instruments indexed under one of our plat names",
        f"- **{len(by_match['variant'])}** more under a variant of one of our names "
        "(\"MISSION VILLAGE BLOCK 2\") and located in Township 12 or 13, Range 25, "
        "where Prairie Village lies",
        f"- **{len(by_match['prefix'])}** variants with no location in the index, and "
        f"**{len(by_match['elsewhere'])}** located elsewhere in the county; both are listed "
        "at the end and left out of the counts",
        "",
        "The original platting is the earliest recorded plat for the subdivision. Plats before "
        "the 1980s are indexed by book and page (\"PLAT 11 39\" is plat book 11, page 39), "
        "often with no legal description. Variant names are marked *(variant)*.",
        "",
        "## Summary",
        "",
        "| Code | Subdivision | Parcels | Original plat | Plats recorded |",
        "|---|---|---:|---|---:|",
    ]
    for (code, sub), plat_list in sorted(subs.items()):
        insts = [e for p in plat_list for e in by_plat[p]]
        first = min(insts, key=lambda e: e["recorded"]) if insts else None
        parcels = sum(names[p]["parcels"] for p in plat_list)
        orig = f"{first['recorded']} ({first['book_page']})" if first else "not found"
        lines.append(f"| {code} | {sub} | {parcels:,} | {orig} | {len(insts)} |")

    lines += ["", "## Plats by subdivision", ""]
    for (code, sub), plat_list in sorted(subs.items()):
        lines.append(f"### {sub} ({code})")
        lines.append("")
        for p in plat_list:
            insts = sorted(by_plat[p], key=lambda e: e["recorded"])
            if not insts:
                lines.append(f"- **{p}**: no recorded plat found under this name")
                continue
            lines.append(f"- **{p}** ({names[p]['parcels']:,} parcels)")
            for e in insts:
                extra = f" — {e['legal']}" if e["legal"] else ""
                variant = f" *(variant: {'; '.join(e['index_names'])})*" if e["match"] == "variant" else ""
                lines.append(f"  - {e['recorded']} {e['doc_type']}, {e['book_page']}{variant}{extra}")
        lines.append("")

    missing = [n for n in names if not by_plat[n]]
    if missing:
        lines += ["## Plat names with no match", "",
                  "No recorded plat was found under these names or a located variant. "
                  "They need a manual look; check the unlocated variants below first.", ""]
        lines += [f"- {n}" for n in missing]
        lines.append("")

    def table(title, intro, rows):
        if not rows:
            return []
        out = [f"## {title}", "", intro, "",
               "| Recorded | Index name | Book/page | Legal | Nearest of our names |",
               "|---|---|---|---|---|"]
        for e in rows:
            out.append(f"| {e['recorded']} | {'; '.join(e['index_names'])} | {e['book_page']} | "
                       f"{e['legal'] or ''} | {e['plat'] or ''} |")
        return out + [""]

    lines += table("Variants with no location (review)",
                   "Index names that start with one of our plat names but have no section, "
                   "township, and range in the index, so they can't be placed. Many older "
                   "ones are likely Prairie Village plats.", by_match["prefix"])
    lines += table("Same-name plats elsewhere in the county",
                   "Index names that start with one of our plat names but are located outside "
                   "Township 12–13, Range 25.", by_match["elsewhere"])
    return "\n".join(lines)
