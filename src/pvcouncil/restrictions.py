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


_NICHOLS = re.compile(r"\bJ\.?\s?C\.?\s+NICHOLS\b|\bNICHOLS\s+(?:INVESTMENT\s+)?CO", re.I)


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _months_between(a: str, b: str) -> int:
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


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
                                                         "engine_terms", "engine_stale")
                          if k in reads.get(e["id"], {})}} for e in out.get("filed_with_plat", [])]

    def cite(term: str, e: dict) -> str:
        """A term, cited to the transcribed page and region when only a transcription found it, or
        to the OCR engine whose adopted reading found it when Tesseract didn't."""
        where = (e.get("transcribed_terms") or {}).get(term)
        if not where:
            return f"{term} (read by {e['engine']})" if term in (e.get("engine_terms") or []) else term
        return f"{term} (transcribed: " + "; ".join(f"p. {w['page']}, region {w['region']}" for w in where) + ")"

    def stale_note(e: dict) -> str:
        n = e.get("stale_transcriptions") or 0
        out = (f" ⚠ {n} transcription{'s' if n > 1 else ''} drawn on an earlier image of this document; "
               "check in the viewer") if n else ""
        if e.get("engine_stale"):
            out += f" ⚠ the {e['engine']} reading is of an earlier image of this document; read it again"
        return out
    filed_rel = [e for e in filed_all if not e.get("unrelated")]
    decl_kinds = ("restrictions", "homes association", "restriction amendment")
    racial = [e for e in filed_rel if "racial restriction" in e.get("features", [])]
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
    lines = [
        "# Prairie Village subdivision restrictions",
        "",
        "Recorded deed restrictions and covenants for each subdivision, from the Johnson County "
        "Recorder of Deeds. Generated by `pvc restriction-survey`; plats are in "
        "[subdivision-plats.md](subdivision-plats.md).",
        "",
        f"- **{len(covered)}** of **{len(subs)}** subdivisions have at least one restriction "
        "instrument found or restrictions written on a plat",
        f"- **{len(filed_all)}** instruments were filed with a plat: recorded the same day with one "
        "of the next few document numbers. The 1941 Prairie Village plat is document 289996 and the "
        "J.C. Nichols Declaration of Restrictions is 289997. Read from their images, "
        f"**{sum(e.get('kind') in decl_kinds for e in filed_rel)}** are restriction or homes "
        f"association declarations covering **{len({p for e in filed_rel if e.get('kind') in decl_kinds for p in e['plats']})}** "
        f"plats; {len(filed_all) - len(filed_rel)} concern other land and are left out below.",
        f"- **{len(racial)}** of those declarations contain a racially restrictive covenant "
        f"({', '.join(sorted({p for e in racial for p in e['plats']}))}). Such covenants have been "
        "unenforceable since Shelley v. Kraemer (1948). This count comes from OCR and covers only the "
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
        "## Summary",
        "",
        "| Code | Subdivision | First plat | Filed with plat | On plat | Indexed restrictions | Candidates |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for (code, sub), pl in sorted(subs.items()):
        f_ = {e["id"] for p in pl for e in filed[p]}
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
        if not (f_ or o_ or t_ or c_):
            continue
        lines += [f"### {sub} ({code})", ""]
        for e in f_:
            what = e.get("kind", e["doc_type"])
            detail = [cite(f, e) for f in e.get("features", []) if f != "homes association assessments"]
            extra = (f"; {e['term']}" if e.get("term") else "") + (f"; {', '.join(detail)}" if detail else "")
            lines.append(f"- **Filed with plat** {', '.join(e['plat_book_pages'])}: {e['recorded']} "
                         f"{e['book_page']} (document {e['docid']}) — **{what}**{extra}"
                         + (f". {e['note']}" if e.get("note") else "") + stale_note(e))
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
