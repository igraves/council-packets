"""Find the recorded deed restrictions and covenants for Prairie Village subdivisions.

Two sources feed this:

1. Typed instruments. The recorder labels some instruments "Declaration of
   Restrictions" (type 63, from 1914 on), "Declaration of Home Owners
   Association" (74) and "Certificate of Release of Prohibitive Covenants"
   (11007). A document-type search works only with no category selected, so
   every instrument of these types is pulled county-wide, a year at a time,
   and cached in deeds/cache/restrictions/ (gitignored: rows carry party names).

2. Plat citations. Some plats cite their restrictions by book and page
   ("Book 22 of Miscellaneous Records, page 306"); deeds/plat_restrictions.json
   (from `pvc plat-ocr`) lists them, and each is looked up directly.

Typed instruments are matched to our subdivisions by the subdivision in their
legal description, or by a party name that is one of our plat names ("KENILWORTH
HOMES ASSOCIATION"). The result, deeds/restrictions.json, has no party names
beyond plat-name parties.

Older restrictions often carry the generic "Miscellaneous" label instead;
finding those (the MISC walk around each plat's date) builds on this.
"""

import json
import re
from collections import defaultdict
from datetime import date

from pvcouncil import plats
from pvcouncil.recorder import RESTRICTION_DOC_TYPES, Recorder

CACHE_DIR = plats.DEEDS_DIR / "cache" / "restrictions"
RESULTS_PATH = plats.DEEDS_DIR / "restrictions.json"
FIRST_YEAR = 1910


def _year_file(doc_type: int, year: int):
    return CACHE_DIR / f"type-{doc_type}-{year}.json"


def fetch_typed(echo=print) -> None:
    """Pull every restriction-type instrument county-wide, one (type, year) at a time."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    this_year = date.today().year
    todo = [(dt, y) for dt in RESTRICTION_DOC_TYPES for y in range(FIRST_YEAR, this_year + 1)
            if not _year_file(dt, y).exists() or y == this_year]
    echo(f"{len(todo)} type-year searches to run")
    if not todo:
        return
    rec = Recorder().login()
    for i, (dt, y) in enumerate(todo, 1):
        rows = rec.search(doc_types=[dt], recorded=(f"01/01/{y}", f"12/31/{y}"))
        _year_file(dt, y).write_text(json.dumps({"doc_type": dt, "year": y, "rows": rows}))
        n = len({r["instrumentReference_id"] for r in rows})
        if n or i % 20 == 0:
            echo(f"[{i}/{len(todo)}] {RESTRICTION_DOC_TYPES[dt]} {y}: {n} instruments")


def fetch_cited(echo=print) -> None:
    """Look up each restriction instrument a plat cites by MISC book and page."""
    from pvcouncil import platocr

    if not platocr.RESULTS_PATH.exists():
        return
    cited = {(c["book"], c["page"]) for p in json.loads(platocr.RESULTS_PATH.read_text())["plats"]
             for c in p["citations"] if c.get("page")}
    todo = [bp for bp in sorted(cited) if not (CACHE_DIR / f"misc-{bp[0]}-{bp[1]}.json").exists()]
    echo(f"{len(cited)} cited MISC book/pages, {len(todo)} to look up")
    if not todo:
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    rec = Recorder().login()
    for book, page in todo:
        rows = rec.search(book=("MISC", book, page))
        (CACHE_DIR / f"misc-{book}-{page}.json").write_text(json.dumps({"book": book, "page": page, "rows": rows}))


MISC_DOC_TYPE = 801  # "Miscellaneous": the label most pre-1976 restrictions carry
WALK_BEFORE, WALK_AFTER = 3, 12  # months around each plat's recording date


def walk_months(manifest: dict) -> list[tuple[int, int]]:
    """Months within the walk window of any pre-1976 Prairie Village plat."""
    months = set()
    for e in manifest["instruments"]:
        if e["match"] not in ("exact", "variant") or e["recorded"] >= "1976":
            continue
        y0, m0 = int(e["recorded"][:4]), int(e["recorded"][5:7])
        for k in range(-WALK_BEFORE, WALK_AFTER + 1):
            y, m = divmod((y0 * 12 + m0 - 1) + k, 12)
            months.add((y, m + 1))
    return sorted(months)


def _month_file(y: int, m: int):
    return CACHE_DIR / "misc" / f"misc-{y}-{m:02d}.json"


def fetch_misc_walk(echo=print) -> None:
    """Pull every 'Miscellaneous' instrument recorded in the walk months, a month at a time."""
    import calendar

    months = walk_months(plats.load_manifest())
    todo = [(y, m) for y, m in months if not _month_file(y, m).exists()]
    echo(f"{len(months)} walk months, {len(todo)} to fetch")
    if not todo:
        return
    _month_file(*todo[0]).parent.mkdir(parents=True, exist_ok=True)
    rec = Recorder().login()
    for i, (y, m) in enumerate(todo, 1):
        last = calendar.monthrange(y, m)[1]
        rows = rec.search(doc_types=[MISC_DOC_TYPE], recorded=(f"{m:02d}/01/{y}", f"{m:02d}/{last}/{y}"))
        _month_file(y, m).write_text(json.dumps({"year": y, "month": m, "rows": rows}))
        if i % 10 == 0 or i == len(todo):
            echo(f"[{i}/{len(todo)}] {y}-{m:02d}: {len({r['instrumentReference_id'] for r in rows})} instruments")


def _instruments(rows: list[dict]) -> dict[str, dict]:
    """Group party rows into instruments; keep the parties for matching only."""
    out: dict[str, dict] = {}
    for r in rows:
        iid = str(r["instrumentReference_id"])
        e = out.setdefault(iid, {
            "id": iid,
            "recorded": r["RecordedDate"],
            "doc_type": r["Value"],
            "book_page": r["ReferenceDisplayValue"],
            "instrument_number": r["InstrumentNumber"],
            "legals": set(),
            "_parties": set(),
        })
        if r["PropertyLegalShort"]:
            e["legals"].add(r["PropertyLegalShort"].strip(" *"))
        for name in (r["LastName"], r["RelatedName"]):
            if name:
                e["_parties"].add(name.strip())
    return out


def load_cached() -> dict[str, dict]:
    insts: dict[str, dict] = {}
    for f in sorted(CACHE_DIR.glob("*.json")):
        cached = json.loads(f.read_text())
        for iid, e in _instruments(cached["rows"]).items():
            if iid in insts:
                insts[iid]["legals"] |= e["legals"]
                insts[iid]["_parties"] |= e["_parties"]
            else:
                e["cited"] = f.name.startswith("misc-")
                insts[iid] = e
    return insts


_ORG_SUFFIX = (r"\s+(?:(?:HOMES?|HOME OWNERS|HOMEOWNERS|PROPERTY OWNERS|OWNERS|IMPROVEMENT|CIVIC)\s+)?"
               r"(?:ASSOCIATION|ASSN|ASSOC|INC|INCORPORATED|COMPANY|CO|CORPORATION|CORP)(?:\s+INC)?")


def _sub_names(legal: str) -> list[str]:
    return [m.strip() for m in re.findall(r"Sub:\s*(.+?)\s*(?=Blk:|Bl:|Lt:|Tract:|other:|Rmrk:|$)", legal)]


def match(insts: dict[str, dict], names: dict[str, dict]) -> list[dict]:
    """Tie instruments to our plats by subdivision legal or plat-name party."""
    # exact plat names first, so "RIDGE VIEW" is RIDGE VIEW, not the replat whose parent it is
    by_loose = {plats.loose(n): n for n in names}
    for n in names:
        by_loose.setdefault(plats.loose(plats.search_term(n)), n)

    def plat_for(text: str, party: bool = False) -> str | None:
        t = plats.loose(text)
        if t in by_loose:
            return by_loose[t]
        for k, n in by_loose.items():
            # the index cuts subdivision names at 30 characters
            if len(text) >= 29 and len(k) > len(t) and k.startswith(t):
                return n
            # a party named for the subdivision: "KENILWORTH HOMES ASSOCIATION", "KENILWORTH INC"
            if party and re.fullmatch(re.escape(k) + _ORG_SUFFIX, t):
                return n
        return None

    matched = []
    for e in insts.values():
        hits, how = set(), set()
        for legal in e["legals"]:
            for sub in _sub_names(legal):
                p = plat_for(sub)
                if p:
                    hits.add(p)
                    how.add("legal")
        for party in e["_parties"]:
            p = plat_for(party, party=True)
            if p:
                hits.add(p)
                how.add("party")
        if e.get("cited"):
            how.add("cited by plat")
        if not hits and not e.get("cited"):
            continue
        matched.append({
            "id": e["id"], "recorded": e["recorded"], "doc_type": e["doc_type"],
            "book_page": e["book_page"], "instrument_number": e["instrument_number"],
            "plats": sorted(hits), "matched_by": sorted(how),
            "subdivision_codes": sorted({names[p]["subdivision_code"] for p in hits}),
            "legal": "; ".join(sorted(e["legals"]))[:300] or None,
        })
    return sorted(matched, key=lambda m: (m["recorded"], m["book_page"]))


_UTILITY = re.compile(r"POWER|LIGHT|TELEPHONE|\bBELL\b|\bGAS\b|WATER|SEWER|PIPE ?LINE|ELECTRIC|CABLE|"
                      r"RAILROAD|RAILWAY|\bKCPL\b|TELEGRAPH|DRAINAGE|PUBLIC SERVICE", re.I)
_COMPANY = re.compile(r"\b(?:INC|CO|COMPANY|CORP|CORPORATION|INVESTMENT|REALTY|LAND|DEVELOPMENT|"
                      r"HOMES|BUILDERS?|CONSTRUCTION|ASSOCIATION|ASSN)\b", re.I)


def developers(typed: list[dict], insts: dict[str, dict], plat_manifest: dict) -> set[str]:
    """Company parties seen on Prairie Village restrictions and plats: the developers to watch for."""
    devs = set()
    for m in typed:
        for p in insts.get(m["id"], {}).get("_parties", ()):
            if _COMPANY.search(p) and not _UTILITY.search(p):
                devs.add(plats.loose(p))
    for e in plat_manifest["instruments"]:
        if e["match"] in ("exact", "variant"):
            for n in e["index_names"]:
                if _COMPANY.search(n):
                    devs.add(plats.loose(n))
    # the Nichols companies developed much of the city under several names
    devs |= {plats.loose(n) for n in ("J C NICHOLS COMPANY", "JC NICHOLS COMPANY", "J C NICHOLS CO",
                                      "J C NICHOLS INVESTMENT COMPANY", "NICHOLS COMPANY J C")}
    return devs


def walk_candidates(names: dict[str, dict], devs: set[str], plat_manifest: dict) -> list[dict]:
    """Score 'Miscellaneous' filings from the walk as possible restrictions.

    Utility easements are dropped. What counts toward a restriction: a party named
    for one of our subdivisions (homes association, company), a subdivision legal
    naming one of our plats, a developer party, an UNKNOWN or PUBLIC grantee (how
    the 1937 declarations at MISC 22/306 and 22/312 are indexed), and a legal with
    no lot (subdivision-wide) or none at all.
    """
    plat_dates = sorted((e["recorded"], e["plat"]) for e in plat_manifest["instruments"]
                        if e["match"] in ("exact", "variant") and e["recorded"] < "1976")
    by_loose = {plats.loose(n): n for n in names}
    out = []
    for f in sorted((CACHE_DIR / "misc").glob("misc-*.json")):
        groups: dict[str, list[dict]] = defaultdict(list)
        for r in json.loads(f.read_text())["rows"]:
            groups[str(r["instrumentReference_id"])].append(r)
        for iid, rows in groups.items():
            parties = [(r["LastName"].strip(), r["FirstName"].strip(), r["Role"]) for r in rows]
            names_only = {p[0] for p in parties} | {r["RelatedName"].strip() for r in rows if r["RelatedName"]}
            if any(_UTILITY.search(n) for n in names_only):
                continue
            legals = {r["PropertyLegalShort"].strip(" *") for r in rows if r["PropertyLegalShort"]}
            hits, why, score = set(), [], 0
            for n in names_only:
                t = plats.loose(n)
                for k, plat in by_loose.items():
                    if t == k or re.fullmatch(re.escape(k) + _ORG_SUFFIX, t):
                        hits.add(plat)
            if hits:
                score += 3
                why.append("party named for the subdivision")
            for legal in legals:
                for sub in _sub_names(legal):
                    p = by_loose.get(plats.loose(sub))
                    if p:
                        hits.add(p)
                        if "subdivision legal" not in why:
                            score += 2
                            why.append("subdivision legal")
            if any(plats.loose(n) in devs or _NICHOLS.search(n) for n in names_only):
                score += 1
                why.append("developer party")
                # filed alongside a plat (the 1941 Prairie Village plat and three Nichols filings
                # with UNKNOWN grantees went on record the same day)
                rec_day = rows[0]["RecordedDate"]
                same_week = [p for d, p in plat_dates if abs(_days_between(d, rec_day)) <= 7]
                if same_week:
                    score += 2
                    why.append("recorded within a week of a plat")
                    hits_week = set(same_week)
                else:
                    hits_week = set()
            else:
                hits_week = set()
            if any(p[0].upper() in ("UNKNOWN", "PUBLIC", "THE PUBLIC") for p in parties):
                score += 2
                why.append("UNKNOWN/PUBLIC party")
            if not legals or all("Lt" not in lg for lg in legals):
                score += 1
                why.append("no lot in legal")
            if score < 5 or not (hits or "developer party" in why):
                continue
            rec = rows[0]["RecordedDate"]
            if not hits and hits_week:
                hits_near = sorted(hits_week)
            elif not hits:
                # tie to the plats recorded within the walk window before this filing
                near = [p for d, p in plat_dates if d <= rec and _months_between(d, rec) <= WALK_AFTER]
                hits_near = sorted(set(near))
            else:
                hits_near = []
            out.append({
                "id": iid, "recorded": rec, "doc_type": rows[0]["Value"],
                "book_page": rows[0]["ReferenceDisplayValue"], "score": score, "why": why,
                "plats": sorted(hits), "nearby_plats": hits_near,
                "legal": "; ".join(sorted(legals))[:200] or None,
            })
    return sorted(out, key=lambda c: (-c["score"], c["recorded"]))


# Releases of racial covenants, keyed by the released declaration's book and page. Kansas House Bill
# 2582 (2006) let a homes association's board amend such a clause out of its declaration; the
# recorder indexes those as a Declaration of Restrictions under the association's name only, so they
# were found by searching association names (all document types) and reading every association-filed
# declaration of 2006-09, and those of 2010 on not indexed to land outside Prairie Village. The
# association names come from each racial subdivision's own instruments and every index row on its
# lots. The 2024 law's Certificates of Release of Prohibitive Covenants (filed by an owner or a city)
# were also checked.
RACIAL_RELEASES = {
    "MISC 26 349": {"recorded": "2007-11-07", "book_page": "OR 200711 001865",
                    "by": "the Prairie Village Homes Association", "law": "House Bill 2582 (2006)"},
}
RELEASES_SEARCHED = "2026-10-20"

# Racial covenants that ended on their own: a fixed term with no renewal clause, read by hand
# (the classifier's "renewal" pattern misses wordings like "automatically shall be continued").
RACIAL_EXPIRED = {
    # J. C. Nichols Company's race-only declaration on Prairie Village Block 36, 1950
    "MISC 43 573": {"expired": "1990-02", "term": "40 years, no renewal"},
}


# Declarations found through the references in other declarations rather than by the survey: the
# Miscellaneous index gives them no legal description, or one lot, so they were found by recording
# date and read. Prairie Village was built in sections; for each, J. C. Nichols Company recorded a
# short declaration subjecting the new lots to the 1941 declaration (MISC 26 349) under its Section
# Four, and the later declarations cite these by serial number. docid is that serial.
FOUND_BY_REFERENCE = [
    {"id": "1732415", "book_page": "MISC 31 53", "recorded": "1946-03-04", "docid": "346348"},
    {"id": "2641720", "book_page": "MISC 36 260", "recorded": "1947-11-10", "docid": "367460"},
    {"id": "2642813", "book_page": "MISC 36 269", "recorded": "1947-11-10", "docid": "367462"},
    {"id": "2642814", "book_page": "MISC 36 272", "recorded": "1947-11-10", "docid": "367463"},
    {"id": "2781189", "book_page": "MISC 37 453", "recorded": "1948-03-31", "docid": "371726"},
    {"id": "2781190", "book_page": "MISC 37 456", "recorded": "1948-03-31", "docid": "371727"},
    {"id": "2781194", "book_page": "MISC 37 473", "recorded": "1948-03-31", "docid": "371729"},
    {"id": "3166519", "book_page": "MISC 40 212", "recorded": "1949-01-03", "docid": "380622"},
    {"id": "3433753", "book_page": "MISC 42 52", "recorded": "1949-08-31", "docid": "388424"},
    {"id": "3433759", "book_page": "MISC 42 54", "recorded": "1949-08-31", "docid": "388425"},
    {"id": "3433698", "book_page": "MISC 42 393", "recorded": "1949-10-20", "docid": "390585"},
    {"id": "3433699", "book_page": "MISC 42 396", "recorded": "1949-10-20", "docid": "390586"},
    {"id": "3433701", "book_page": "MISC 42 405", "recorded": "1949-10-20", "docid": "390587"},
    # cited in the margin of MISC 42 54 ("Modif. of Restr. #746475, Bk 161 Pg 172")
    {"id": "3239007", "book_page": "MISC 161 172", "recorded": "1966-01-12", "docid": "746475"},
]
for _e in FOUND_BY_REFERENCE:
    _e.update(plats=["PRAIRIE VILLAGE"], plat_book_pages=[], source="reference")

# Declarations the typed search matched to a subdivision but that weren't filed with its plat, so
# they're read only when listed here.
TYPED_EXTRA = [
    # J. C. Nichols Company's restrictions for Prairie Hills Blocks 1-12, eight days after the plat
    # (PLAT 14 33); indexed only to Block 12 Lot 1
    {"id": "4093235", "book_page": "MISC 46 137", "recorded": "1950-07-08", "docid": "402618",
     "doc_type": "Declaration of Restrictions", "plats": ["PRAIRIE HILLS"]},
    # the owners' Restriction Agreement for Prairie Woods Lots 1-8 and 14-31, which annulled MISC 24 273;
    # indexed as Miscellaneous with no legal, found under the developer's name
    {"id": "4591212", "book_page": "MISC 27 187", "recorded": "1941-10-06", "docid": "297222",
     "doc_type": "Restriction Agreement", "plats": ["PRAIRIE WOODS"]},
]

# Declarations annulled by a later instrument, keyed by book and page: the later one replaces them.
ANNULLED = {
    "MISC 24 273": {"by": "MISC 27 187", "recorded": "1941-10-06",
                    "note": "the 1941 Restriction Agreement annulled it \"as though said Declaration of Restrictions "
                            "had never been executed\" and put new restrictions, the racial covenant among them, on "
                            "Lots 1-8 and 14-31"},
}
for _e in TYPED_EXTRA:
    _e.update(plat_book_pages=[], source="typed")


def filed_entries(out: dict) -> list[dict]:
    """The instruments read as declarations: those filed with a plat, FOUND_BY_REFERENCE and
    TYPED_EXTRA."""
    have = {e["id"] for e in out.get("filed_with_plat", [])}
    return out.get("filed_with_plat", []) + [e for e in FOUND_BY_REFERENCE + TYPED_EXTRA if e["id"] not in have]


_NICHOLS = re.compile(r"\bJ\.?\s?C\.?\s+NICHOLS\b|\bNICHOLS\s+(?:INVESTMENT\s+)?CO", re.I)


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _months_between(a: str, b: str) -> int:
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def _reference_section() -> tuple[list[str], set[str]]:
    """Report lines for racial covenants carried to land by a declaration that makes it subject to
    an earlier one (subdivmap's racial_ref), and the subdivisions they are in."""
    from pvcouncil import subdivmap

    subs = subdivmap.subdivision_records()
    rows = [(s["name"], d) for s in subs.values() for d in s["declarations"] if d.get("racial_ref")]
    if not rows:
        return [], set()
    bases = sorted({d["racial_ref"]["declaration"] for _, d in rows})
    lines = [
        "## Racial covenants carried by reference",
        "",
        "A declaration can put a racial covenant on land without stating it, by making the land "
        "subject to all the terms of an earlier declaration that has one. The 1941 Prairie Village "
        "declaration (MISC 26 349, Blocks 1–5) reserved in its Section Four the right to add land "
        "to it. J. C. Nichols Company used that right section by section: each later plat got a "
        "short declaration making its lots subject to MISC 26 349 \"as though said lots … had been "
        "described in said original Declaration\", and a longer one with additional restrictions "
        "that cites it. Several of these short declarations are indexed with no legal description "
        "and were found from the serial numbers the longer ones cite.",
        "",
        "| Subdivision | Declaration | Recorded | Blocks | Covenant carried from |",
        "|---|---|---|---|---|",
    ]
    for name, d in sorted(rows, key=lambda r: (r[0], r[1]["recorded"])):
        blocks = subdivmap._blocks_label(d["blocks"]) if d["blocks"] else "all"
        lines.append(f"| {name.title()} | {d['book_page']} ({d['kind']}) | {d['recorded'][:10]} | {blocks} "
                     f"| {d['racial_ref']['declaration']} ({d['racial_ref']['recorded'][:4]}) |")
    lines.append("")
    for b in bases:
        r = RACIAL_RELEASES.get(b)
        if r:
            lines += [f"{b}'s racial covenant was released {r['recorded']} by {r['by']} under {r['law']} "
                      f"({r['book_page']}). The release repeals the section for \"all properties subject to the "
                      "declaration\", but its recitals list only the lots that declaration first covered, so "
                      "whether it reaches land added by reference is unclear. The map shows that land as "
                      "*by reference*.", ""]
    return lines, {name.split(",")[0].upper() for name, _ in rows}


def _preplat_section() -> tuple[list[str], dict[str, list[dict]]]:
    """Report lines for covenants put on acreage before it was platted (pvc preplat), and those
    covenants by the subdivision name now on their land. Empty until pvc preplat has run."""
    from pvcouncil import preplat

    if not (preplat.RESULTS_PATH.exists() and preplat.OVERLAY_PATH.exists()):
        return [], {}
    found = json.loads(preplat.RESULTS_PATH.read_text())
    insts = found["instruments"]
    covs = [f["properties"] for f in json.loads(preplat.OVERLAY_PATH.read_text())["features"]]
    by_sub = defaultdict(list)
    lines = [
        "## Before it was platted",
        "",
        "Some land was put under a racial covenant before it was platted, by its owners or by the "
        "developer who was buying it. Such instruments are indexed to the bare land (section, township "
        "and range) rather than to a subdivision, so the search by subdivision name above cannot find "
        "them. `pvc preplat` dates each 40-acre tract our subdivisions occupy by its first plat and "
        "reads every Miscellaneous instrument indexed to bare land in that tract in the "
        f"{found['years_before']} years before: **{len(insts)}** instruments, "
        f"**{sum(bool(e.get('racial')) for e in insts)}** of them with a racial covenant. Each was read "
        "from its image and its land placed from the description it gives (the index's quarter-section "
        "is often wrong). All ran for a fixed term with no renewal clause, so all have ended; the land "
        "they covered is shown on the map as dashed outlines.",
        "",
    ]
    for p in sorted(covs, key=lambda p: p["recorded"]):
        now = [n.split(",")[0] for n in json.loads(p["now"])]
        for n in dict.fromkeys(now):
            by_sub[n].append(p)
        where = (f"Land now in {', '.join(dict.fromkeys(n.title() for n in now))}." if now else "")
        lines.append(f"- **{p['recorded']} {p['book_page']}**, by {p['by']}: {p['summary']} "
                     f"Ended {p['ended']} ({p['term']}). {where}".rstrip())
    lines.append("")
    return lines, by_sub


def _deed_chain_section() -> tuple[list[str], set[str]]:
    """Report lines for the sample lots whose deeds pvc deed-chain followed, and the subdivisions
    they are in. Empty until pvc deed-chain has run."""
    from pvcouncil import deedchain

    if not deedchain.RESULTS_PATH.exists():
        return [], set()
    found = json.loads(deedchain.RESULTS_PATH.read_text())
    rows, lots = found["deeds"], found["lots"]
    dev = [r for r in rows if r["approval_clause"]]
    costs = sorted({r["minimum_cost"] for r in dev if r["minimum_cost"]}, reverse=True)
    olm = sum(r["read_by"] == "olmOCR-2" for r in rows)
    early = [r for r in rows if r["recorded"] < "1960" and r["read_by"] != "olmOCR-2"]
    by_block = defaultdict(list)
    for r in rows:
        by_block[(r["subdivision"], r["block"])].append(r)
    lines = [
        "## Deeds on individual lots",
        "",
        "Racial language is sometimes said to have been written into the deeds of individual houses, not "
        "only into a subdivision's recorded declaration. `pvc deed-chain` tests this on sample lots by "
        "following each one's recorded history (the recorder's search by subdivision, block and lot) from "
        "the developer's first sale to today, and reading every deed in it: "
        f"**{len(lots)}** lots in {len(by_block)} blocks, **{len(rows)}** deeds from "
        f"{min(r['recorded'] for r in rows)[:4]} to {max(r['recorded'] for r in rows)[:4]}. Every deed was "
        f"read with Tesseract, and the {olm} recorded before 1960 also with olmOCR-2 7B"
        + (f" (except {', '.join(r['book_page'] for r in early)}, which it could not read)" if early else "")
        + f". **{sum(r['racial'] for r in rows)}** of them contain racial language.",
        "",
        f"The developer's deeds ({len(dev)}, from {min(r['recorded'] for r in dev)[:4]} to "
        f"{max(r['recorded'] for r in dev)[:4]}) convey the lot \"subject to the covenants, restrictions, "
        "reservations and easements now of record\", that is, the subdivision's recorded declarations, and "
        "add only their own printed terms: for forty years no house or outbuilding may be built until the "
        "J. C. Nichols Company approves its plans, and in "
        f"{sum(bool(r['minimum_cost']) for r in dev)} of them a minimum cost "
        f"({' or '.join(f'${c:,}' for c in costs)}). The other deeds, nearly all resales, cite the restrictions of record "
        f"({sum(r['cites_restrictions'] for r in rows if not r['approval_clause'])} of "
        f"{len(rows) - len(dev)}) or say nothing of them. In this sample a racial covenant reached a lot "
        "only through the declaration its deeds cite.",
        "",
        "| Subdivision | Block | Lots | Deeds | First sale | With approval clause | Minimum cost |",
        "|---|---|---|---|---|---|---|",
    ]
    for (sub, block), rs in sorted(by_block.items(), key=lambda kv: (kv[0][0], int(kv[0][1]))):
        lts = sorted({r["lot"] for r in rs}, key=lambda x: int(x or 0))
        cs = sorted({r["minimum_cost"] for r in rs if r["approval_clause"] and r["minimum_cost"]})
        lines.append(f"| {sub.title()} | {block} | {', '.join(lts) if any(lts) else 'whole block'} | {len(rs)} | "
                     f"{min(r['recorded'] for r in rs)[:4]} | {sum(r['approval_clause'] for r in rs)} | "
                     f"{', '.join(f'${c:,}' for c in cs) or '—'} |")
    lines.append("")
    return lines, {r["subdivision"] for r in rows}


def render_markdown(out: dict, manifest: dict) -> str:
    """Per-subdivision report: restrictions filed with the plat, written on the plat, typed
    restriction instruments, and Miscellaneous candidates still to read."""
    from pvcouncil import platocr

    names = manifest["plats"]
    on_plat = {}
    if platocr.RESULTS_PATH.exists():
        for p in json.loads(platocr.RESULTS_PATH.read_text())["plats"]:
            if p["terms"] and ({"restrictions", "covenants"} & set(p["terms"])):
                on_plat.setdefault(p["plat"], []).append(p)
    subs = defaultdict(list)
    for n, info in names.items():
        subs[(info["subdivision_code"], info["subdivision"])].append(n)

    def by_plat(items, key="plats"):
        d = defaultdict(list)
        for e in items:
            for p in e.get(key) or []:
                d[p].append(e)
        return d

    from pvcouncil import restrictread

    reads = {}
    if restrictread.RESULTS_PATH.exists():
        reads = {r["id"]: r for r in json.loads(restrictread.RESULTS_PATH.read_text())["read"]}
    filed_all = [{**e, **{k: reads[e["id"]][k] for k in ("kind", "features", "term", "note", "unrelated",
                                                         "transcribed_terms", "stale_transcriptions", "engine",
                                                         "engine_terms", "engine_stale", "kind_engine",
                                                         "unadopted_terms")
                          if k in reads.get(e["id"], {})}}
                 for e in out.get("filed_with_plat", []) + [e for e in TYPED_EXTRA
                                                            if e["id"] not in {f["id"] for f in out.get("filed_with_plat", [])}]]

    def cite(term: str, e: dict) -> str:
        """A term, cited to the transcribed page and region when only a transcription found it, or
        to the OCR engine whose adopted reading found it when Tesseract didn't."""
        where = (e.get("transcribed_terms") or {}).get(term)
        if not where:
            if term in (e.get("unadopted_terms") or []):  # an olmOCR reading not yet adopted in the viewer
                return f"{term} (read by {e['kind_engine']})"
            return f"{term} (read by {e['engine']})" if term in (e.get("engine_terms") or []) else term
        return f"{term} (transcribed: " + "; ".join(f"p. {w['page']}, region {w['region']}" for w in where) + ")"

    def stale_note(e: dict) -> str:
        n = e.get("stale_transcriptions") or 0
        out = (f" ⚠ {n} transcription{'s' if n > 1 else ''} drawn on an earlier image of this document; "
               "check in the viewer") if n else ""
        if e.get("engine_stale"):
            out += f" ⚠ the {e['engine']} reading is of an earlier image of this document; read it again"
        return out
    n_with_plat = sum(e.get("source") != "typed" for e in filed_all)
    filed_rel = [e for e in filed_all if not e.get("unrelated")]
    decl_kinds = ("restrictions", "homes association", "restriction amendment")
    racial = [e for e in filed_rel if "racial restriction" in e.get("features", []) and e["book_page"] not in ANNULLED]
    n_released = sum(e["book_page"] in RACIAL_RELEASES for e in racial)
    annulled = [e for e in filed_rel if "racial restriction" in e.get("features", []) and e["book_page"] in ANNULLED]

    def release_note(e: dict) -> str:
        if "racial restriction" not in e.get("features", []):
            return ""
        a = ANNULLED.get(e["book_page"])
        if a:
            return f". **Annulled** {a['recorded']} by {a['by']}: {a['note']}"
        r = RACIAL_RELEASES.get(e["book_page"])
        if not r:
            return f". Racial covenant: no release found (recorder searched 2006 to {RELEASES_SEARCHED})"
        return f". Racial covenant **released** {r['recorded']} by {r['by']} under {r['law']}, {r['book_page']}"
    filed = by_plat(filed_rel)
    typed = by_plat(out.get("instruments", []))
    cands = by_plat([c for c in out.get("misc_candidates", []) if c["plats"]])
    first_plat = {}
    for e in manifest["instruments"]:
        if e["match"] in ("exact", "variant"):
            for p in e.get("plats") or [e["plat"]]:
                if p not in first_plat or e["recorded"] < first_plat[p]:
                    first_plat[p] = e["recorded"]

    plat_rows = (json.loads(platocr.RESULTS_PATH.read_text())["plats"] if platocr.RESULTS_PATH.exists() else [])
    tr_rows = [e for e in filed_rel if e.get("transcribed_terms") is not None] + \
              [r for r in plat_rows if r.get("transcribed")]
    n_tr = len([e for e in filed_rel if reads.get(e["id"], {}).get("transcribed")]) + len(
        [r for r in plat_rows if r.get("transcribed")])
    n_tr_terms = sum(len(e.get("transcribed_terms") or {}) for e in tr_rows)
    eng_rows = [e for e in filed_rel if e.get("engine")] + [r for r in plat_rows if r.get("engine")]
    n_eng_terms = sum(len(e.get("engine_terms") or []) for e in eng_rows)
    covered = [k for k, pl in subs.items() if any(filed[p] or on_plat.get(p) or typed[p] for p in pl)]
    preplat_lines, preplat_by_sub = _preplat_section()
    chain_lines, chain_subs = _deed_chain_section()
    ref_lines, ref_subs = _reference_section()
    lines = [
        "# Prairie Village subdivision restrictions",
        "",
        "Recorded deed restrictions and covenants for each subdivision, from the Johnson County "
        "Recorder of Deeds. Generated by `pvc restriction-survey`; plats are in "
        "[subdivision-plats.md](subdivision-plats.md).",
        "",
        f"- **{len(covered)}** of **{len(subs)}** subdivisions have at least one restriction "
        "instrument found or restrictions written on a plat",
        f"- **{n_with_plat}** instruments were filed with a plat: recorded the same day with one "
        "of the next few document numbers. The 1941 Prairie Village plat is document 289996 and the "
        "J.C. Nichols Declaration of Restrictions is 289997. "
        + (f"{len(filed_all) - n_with_plat} more, recorded later and found in the recorder's index, "
           f"{'was' if len(filed_all) - n_with_plat == 1 else 'were'} read the same way. "
           if len(filed_all) > n_with_plat else "")
        + "Read from their images, "
        f"**{sum(e.get('kind') in decl_kinds for e in filed_rel)}** are restriction or homes "
        f"association declarations covering **{len({p for e in filed_rel if e.get('kind') in decl_kinds for p in e['plats']})}** "
        f"plats; {len(filed_all) - len(filed_rel)} concern other land and are left out below.",
        f"- **{len(racial)}** of those declarations contain a racially restrictive covenant "
        f"({', '.join(sorted({p for e in racial for p in e['plats']}))}). Such covenants have been "
        "unenforceable since Shelley v. Kraemer (1948). "
        f"**{n_released}** of them {'has' if n_released == 1 else 'have'} been formally released of record: "
        "Kansas House Bill 2582 (2006) let a homes association's board amend the clause out, and a 2024 "
        "law lets an owner or the city file a certificate of release, but a search of the recorder from "
        f"2006 to {RELEASES_SEARCHED} found no release for the others (see each declaration below). "
        "That search covered every filing under the subdivisions' homes association names and every "
        "association-filed declaration that could concern Prairie Village land. "
        + "".join(f"{e['book_page']} ({', '.join(p.title() for p in e['plats'])}, {e['recorded'][:4]}) is not "
                  f"counted: {ANNULLED[e['book_page']]['by']} annulled it in {ANNULLED[e['book_page']]['recorded'][:4]} "
                  "and repeated its racial covenant. " for e in annulled)
        + "This count comes from OCR and covers only the "
        "declarations it could read; about 20 Kansas City Investment Company declarations from "
        "1952–57 are low-resolution photostat negatives and were classified by eye from their "
        "headings only.",
        *([f"- **{n_tr}** document{'s have' if n_tr != 1 else ' has'} hand-checked transcriptions of regions OCR couldn't read "
           f"(made in `pvc deed-viewer`); **{n_tr_terms}** terms below were found only in a "
           "transcription and are cited to its page and region."] if n_tr else []),
        *([f"- **{len(eng_rows)}** document{'s are' if len(eng_rows) != 1 else ' is'} read with an OCR engine "
           f"other than Tesseract ({', '.join(sorted({e['engine'] for e in eng_rows}))}), adopted in "
           "`pvc deed-viewer` after checking against the image; "
           f"**{n_eng_terms}** term{'s' if n_eng_terms != 1 else ''} below {'were' if n_eng_terms != 1 else 'was'} "
           "found only in that reading and say which engine read them."] if eng_rows else []),
        f"- **{sum(len(v) for v in on_plat.values())}** plats state restrictions or covenants on "
        "their face (found by OCR of the plat images)",
        f"- **{len(out.get('instruments', []))}** instruments indexed as a Declaration of "
        "Restrictions or homes association declaration name one of our subdivisions",
        "",
        "How to read it: *filed with the plat* is the strongest evidence that an instrument is the "
        "subdivision's original restrictions, though some may be other papers filed the same day "
        "(easements to utilities are left out). Before about 1976 the recorder indexed most "
        "restrictions as \"Miscellaneous\", so the label alone doesn't say what an instrument is; "
        "the images settle it.",
        "",
        *preplat_lines,
        *ref_lines,
        *chain_lines,
        "## Summary",
        "",
        "| Code | Subdivision | First plat | Filed with plat | On plat | Indexed restrictions | Candidates |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for (code, sub), pl in sorted(subs.items()):
        f_ = {e["id"] for p in pl for e in filed[p] if e.get("source") != "typed"}
        o_ = {e["book_page"] for p in pl for e in on_plat.get(p, [])}
        t_ = {e["id"] for p in pl for e in typed[p]}
        c_ = {e["id"] for p in pl for e in cands[p]}
        fp = min((first_plat[p] for p in pl if p in first_plat), default="")
        lines.append(f"| {code} | {sub} | {fp} | {len(f_)} | {len(o_)} | {len(t_)} | {len(c_)} |")

    lines += ["", "## By subdivision", ""]
    for (code, sub), pl in sorted(subs.items()):
        f_ = sorted({e["id"]: e for p in pl for e in filed[p]}.values(), key=lambda e: e["recorded"])
        o_ = sorted({e["book_page"]: e for p in pl for e in on_plat.get(p, [])}.values(), key=lambda e: e["recorded"])
        t_ = sorted({e["id"]: e for p in pl for e in typed[p]}.values(), key=lambda e: e["recorded"])
        c_ = sorted({e["id"]: e for p in pl for e in cands[p]}.values(), key=lambda e: -e["score"])
        pre = preplat_by_sub.get(sub.upper(), [])
        if not (f_ or o_ or t_ or c_ or pre):
            continue
        lines += [f"### {sub} ({code})", ""]
        if sub.upper() in ref_subs:
            lines.append("- **[Racial covenants carried by reference](#racial-covenants-carried-by-reference)**: "
                         "declarations that put lots here under an earlier declaration's racial covenant")
        if sub.upper() in chain_subs:
            lines.append("- **[Deeds on individual lots](#deeds-on-individual-lots)**: sample lots' deeds "
                         "followed from the first sale to today")
        for p in pre:
            lines.append(f"- **[Before it was platted](#before-it-was-platted)**: {p['recorded']} {p['book_page']}, "
                         f"a racial covenant put on this land by {p['by']} (ended {p['ended']})")
        for e in f_:
            what = e.get("kind", e["doc_type"])
            detail = [cite(f, e) for f in e.get("features", []) if f != "homes association assessments"]
            extra = (f"; {e['term']}" if e.get("term") else "") + (f"; {', '.join(detail)}" if detail else "")
            label = ("**Declaration** (from the recorder's index)" if e.get("source") == "typed"
                     else f"**Filed with plat** {', '.join(e['plat_book_pages'])}")
            lines.append(f"- {label}: {e['recorded']} "
                         f"{e['book_page']} (document {e['docid']}) — **{what}**{extra}"
                         + (f". {e['note']}" if e.get("note") else "") + release_note(e) + stale_note(e))
        for e in o_:
            cites = ", ".join(f"MISC {c['book']}/{c['page'] or '?'}" for c in e["citations"])
            lines.append(f"- **On the plat** {e['book_page']} ({e['recorded']}): "
                         f"{', '.join(cite(t, e) for t in e['terms'])}" + (f"; cites {cites}" if cites else "")
                         + stale_note(e))
        shown = {e["id"] for e in f_}
        t_rest = [e for e in t_ if e["id"] not in shown]
        for e in t_rest[:12]:
            lines.append(f"- {e['recorded']} {e['doc_type']}, {e['book_page']}"
                         + (f" — {e['legal'][:80]}" if e.get("legal") else ""))
        if len(t_rest) > 12:
            lines.append(f"- … and {len(t_rest) - 12} more indexed restriction instruments")
        c_rest = [e for e in c_ if e["id"] not in shown]
        if c_rest:
            lines.append(f"- Candidates to read ({len(c_rest)}): "
                         + "; ".join(f"{e['book_page']} ({e['recorded']})" for e in c_rest[:8])
                         + (" …" if len(c_rest) > 8 else ""))
        lines.append("")
    return "\n".join(lines)


FILED_WITH_GAP = 5  # document numbers after the plat's that still count as filed with it


def filed_with_plats(manifest: dict) -> list[dict]:
    """Instruments recorded the same day as a Prairie Village plat with the next few document
    numbers. Developers recorded a plat's restrictions right after the plat: the 1941
    Prairie Village plat is DocId 289996 and its Declaration of Restrictions 289997."""
    pv = {int(e["docid"]): e for e in manifest["instruments"]
          if e["match"] in ("exact", "variant") and e.get("docid")}
    found: dict[str, dict] = {}
    for f in sorted(CACHE_DIR.rglob("*.json")):
        for r in json.loads(f.read_text())["rows"]:
            m = re.search(r"DOCID:\s*(\d+)", r["Remarks"] or "", re.I)
            if not m:
                continue
            d = int(m.group(1))
            for k in range(1, FILED_WITH_GAP + 1):
                p = pv.get(d - k)
                if p and p["recorded"] == r["RecordedDate"] and not _UTILITY.search(r["LastName"] or ""):
                    iid = str(r["instrumentReference_id"])
                    e = found.setdefault(iid, {
                        "id": iid, "recorded": r["RecordedDate"], "doc_type": r["Value"],
                        "book_page": r["ReferenceDisplayValue"], "docid": d,
                        "instrument_number": r["InstrumentNumber"], "plat_book_pages": [],
                        "plats": [], "utility": False,
                    })
                    if p["book_page"] not in e["plat_book_pages"]:
                        e["plat_book_pages"].append(p["book_page"])
                    for pl in p.get("plats") or [p["plat"]]:
                        if pl not in e["plats"]:
                            e["plats"].append(pl)
            # an easement filed the same moment is not a restriction; mark it if any party is a utility
            iid = str(r["instrumentReference_id"])
            if iid in found and (_UTILITY.search(r["LastName"] or "") or _UTILITY.search(r["RelatedName"] or "")):
                found[iid]["utility"] = True
    return sorted((e for e in found.values() if not e["utility"]),
                  key=lambda e: (e["recorded"], e["docid"]))


def build() -> dict:
    manifest = plats.load_manifest()
    names = manifest["plats"]
    insts = load_cached()
    matched = match(insts, names)
    by_type = defaultdict(int)
    for e in insts.values():
        by_type[e["doc_type"]] += 1
    out = {"searched": dict(by_type), "filed_with_plat": filed_with_plats(manifest), "instruments": matched}
    if (CACHE_DIR / "misc").exists():
        out["misc_candidates"] = walk_candidates(names, developers(matched, insts, manifest), manifest)
    RESULTS_PATH.write_text(json.dumps(out, indent=1) + "\n")
    return out
