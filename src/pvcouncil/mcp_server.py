"""MCP server for Prairie Village: council documents, votes, budgets, parcels, plats, and the
recorded restrictions and covenants on the city's subdivisions, with page images.

Run over stdio for Claude Desktop or Claude Code (`pvc mcp`), or over streamable HTTP on
localhost (`pvc mcp --http`) for a client that connects by URL. Owner names and mailing
addresses from the local owners file are returned only when PVC_MCP_OWNERS=1.
"""

import json
import os
import re
from collections import defaultdict

from mcp.server.fastmcp import FastMCP, Image

from pvcouncil import config, mcpassets

OWNERS = os.environ.get("PVC_MCP_OWNERS") == "1"

mcp = FastMCP(
    "Prairie Village",
    instructions=(
        "Prairie Village, Kansas: city council documents (meeting packets, minutes, city reports), "
        "voting records, budgets, 2026 parcel and property values, and the recorded plats, deed "
        "restrictions, and covenants (including racially restrictive covenants) on its subdivisions.\n"
        "- Council: 'search' finds passages, 'ask' answers with citations, 'votes' and 'budget' "
        "query extracted records; 'council_page_image' shows a cited packet page.\n"
        "- Property: 'parcels' looks up or summarizes parcels; 'subdivisions' lists subdivisions "
        "with their restriction status.\n"
        "- Covenants: 'subdivision' gives one subdivision's plats, declarations, releases, and "
        "covenants; 'restriction_documents', 'preplat_covenants', and 'deed_chain' list recorded "
        "instruments.\n"
        "- Recorded documents are cited by book and page (e.g. 'MISC 26 349', 'PLAT 12 41'). "
        "'document_info' says what each dataset found in one, 'document_text' gives its OCR text, "
        "and 'document_page_image' shows a page; pass a region to zoom into a large plat sheet. "
        "OCR of old scans is imperfect: check a claim against the page image before relying on it.\n"
        "- 'subdivision_map' draws subdivisions colored by restriction status."
    ),
)


@mcp.tool()
def search(
    query: str,
    num_results: int = 10,
    doc_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    keyword: bool = False,
) -> str:
    """Search Prairie Village city council documents.

    Args:
        query: Search query text
        num_results: Number of results to return (default 10)
        doc_type: Filter by document type: "packet", "minutes", "archive" (city reports: budgets, ACFRs, quarterly financials, police reports), or "other"
        date_from: Filter to documents after this date (YYYY-MM-DD)
        date_to: Filter to documents before this date (YYYY-MM-DD)
        keyword: If true, use exact keyword matching instead of semantic search
    """
    from pvcouncil.search import semantic_search, keyword_search

    if keyword:
        results = keyword_search(
            query, n=num_results, doc_type=doc_type,
            date_from=date_from, date_to=date_to,
        )
        mode = "keyword"
    else:
        results = semantic_search(
            query, n=num_results, doc_type=doc_type,
            date_from=date_from, date_to=date_to,
        )
        mode = "semantic"

    if not results:
        return "No results found."

    lines = [f"{len(results)} results ({mode} search; council_page_image shows a result's page):\n"]
    for i, r in enumerate(results):
        lines.append(f"[{i+1}] {r.source_file}")
        lines.append(f"    Date: {r.meeting_date}  |  Type: {r.doc_type}  |  Page: {r.page_number}")
        if r.score:
            lines.append(f"    Score: {r.score:.4f}")
        lines.append(f"    {r.text[:400]}...")
        lines.append("")
    return "\n".join(lines)


@mcp.tool()
def ask(
    question: str,
    doc_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    num_chunks: int = config.RAG_N_CHUNKS,
) -> str:
    """Ask a question about Prairie Village city council documents. Returns a synthesized answer with citations.

    Args:
        question: The question to answer
        doc_type: Filter by document type: "packet", "minutes", "archive" (city reports: budgets, ACFRs, quarterly financials, police reports), or "other"
        date_from: Filter to documents after this date (YYYY-MM-DD)
        date_to: Filter to documents before this date (YYYY-MM-DD)
        num_chunks: Number of context chunks to retrieve (default 15)
    """
    from pvcouncil.rag import ask as rag_ask

    result = rag_ask(
        question, n_chunks=num_chunks,
        doc_type=doc_type, date_from=date_from, date_to=date_to,
    )

    lines = [result["answer"], "", f"--- Sources ({result['chunks_used']} chunks retrieved) ---"]
    for s in result["sources"]:
        lines.append(f"  {s['date']}  |  {s['doc_type']}  |  {s['file']}")
    return "\n".join(lines)


@mcp.tool()
def list_documents(
    doc_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    """List all locally available council documents (PDFs).

    Args:
        doc_type: Filter by type: "packet", "minutes", "archive", or "other"
        date_from: Only show documents on or after this date (YYYY-MM-DD)
        date_to: Only show documents on or before this date (YYYY-MM-DD)
    """
    from pvcouncil.index import discover_docs

    docs = discover_docs()

    if doc_type:
        docs = [d for d in docs if d["doc_type"] == doc_type]
    if date_from:
        docs = [d for d in docs if d["meeting_date"] >= date_from]
    if date_to:
        docs = [d for d in docs if d["meeting_date"] <= date_to]

    if not docs:
        return "No documents found matching filters."

    lines = [f"{len(docs)} documents:\n"]
    for d in docs:
        lines.append(f"  {d['meeting_date']}  |  {d['doc_type']:7s}  |  {d['filename']}")
    return "\n".join(lines)


@mcp.tool()
def votes(
    member: str | None = None,
    side: str | None = None,
    result: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    max_results: int = 50,
) -> str:
    """Query extracted council voting records (who voted for/against what, and the outcome).

    Args:
        member: Only votes involving this member (name substring)
        side: With member: restrict to how they voted — "yes", "no", or "abstain"
        result: Filter by outcome: "passed", "failed", "tabled", "withdrawn", "unknown"
        search: Substring match on what was voted on
        date_from: Only meetings on or after this date (YYYY-MM-DD)
        date_to: Only meetings on or before this date (YYYY-MM-DD)
        max_results: Maximum records to return (default 50, most recent first)
    """
    from pvcouncil.votes import VOTES_PATH, query_votes

    if not VOTES_PATH.exists():
        return "No vote data extracted yet — run `pvc extract-votes` first."

    records = query_votes(member=member, side=side, result=result,
                          search=search, date_from=date_from, date_to=date_to)
    if not records:
        return "No matching votes."

    shown = records[-max_results:]
    lines = [f"{len(records)} votes matched (showing {len(shown)}, oldest first):\n"]
    for v in shown:
        tally = f" {v['tally']}" if v["tally"] else ""
        lines.append(f"{v['meeting_date']}  {v['result'].upper()}{tally}  "
                     f"[{v['motion_type']}] {v['item']}")
        for key, label in (("no", "No"), ("abstain", "Abstain"), ("yes", "Yes")):
            if v[key]:
                lines.append(f"    {label}: {', '.join(v[key])}")
    return "\n".join(lines)


@mcp.tool()
def budget(year: int | None = None, compare_to: int | None = None) -> str:
    """Prairie Village annual budgets extracted from council packets.

    With no arguments: headline figures (total budget, General Fund, property
    tax, mill levy, revenue-neutral rate, assessed valuation, reserves) for
    every budget year. With year: that year compared with the prior year,
    including which expense categories and departments drove the change and
    the reasons staff cited, with source pages.

    Args:
        year: Budget year to examine (e.g. 2027)
        compare_to: Year to compare against (default: year - 1)
    """
    from pvcouncil.budget import BUDGETS_PATH, compare, render_compare, render_trend

    if not BUDGETS_PATH.exists():
        return "No budget data extracted yet — run `pvc extract-budgets` first."
    if year is None:
        return render_trend()
    try:
        return render_compare(compare(compare_to or year - 1, year))
    except KeyError as e:
        return e.args[0]


@mcp.tool()
def stats() -> str:
    """Show index statistics: number of chunks, PDFs on disk, and storage location."""
    from pvcouncil.index import get_collection, discover_docs

    collection = get_collection()
    count = collection.count()
    docs = discover_docs()

    return (
        f"Collection:   {config.COLLECTION_NAME}\n"
        f"Chunks:       {count}\n"
        f"PDFs on disk: {len(docs)}\n"
        f"ChromaDB dir: {config.CHROMA_DIR}"
    )


@mcp.tool()
def council_page_image(filename: str, page: int, region: list[float] | None = None,
                       max_side: int = mcpassets.DEFAULT_SIDE) -> list:
    """Image of one page of a council packet, minutes, or city report, as search results cite it.

    Args:
        filename: The document's filename from a search result (e.g. "2024-05-06_packet_City_Council.pdf")
        page: Page number (1-based), as the search result gives it
        region: Optional [x0, y0, x1, y1] as fractions of the page (0–1) to zoom into, e.g. [0, 0.5, 1, 1] for the bottom half
        max_side: Longest side of the image in pixels (default 1568, max 2400)
    """
    doc = mcpassets.council_doc(filename)
    if not doc:
        return [f"No local document named {filename!r}. Use list_documents to find filenames."]
    data, info = mcpassets.render(doc["path"], page, region=region, max_side=max_side)
    return [f"{doc['filename']} ({doc['doc_type']}, {doc['meeting_date']}), page {info['page']} of {info['pages']}, "
            f"{info['width_px']}×{info['height_px']} px", Image(data=data, format="jpeg")]


# ---------------------------------------------------------------------------
# Parcels
# ---------------------------------------------------------------------------

def _parcels(year: int | None):
    from pvcouncil import parcels as pm

    records = pm.load(year)
    if not OWNERS:
        for r in records:
            r.update(dict.fromkeys(pm.OWNER_COLUMNS))
    return pm, records


@mcp.tool()
def parcels(
    address: str | None = None,
    subdivision: str | None = None,
    block: str | None = None,
    zoning: str | None = None,
    property_class: str | None = None,
    use: str | None = None,
    tax_unit: str | None = None,
    built_after: int | None = None,
    built_before: int | None = None,
    min_value: int | None = None,
    max_value: int | None = None,
    corp_or_llc: bool = False,
    out_of_state: bool = False,
    mail_state: str | None = None,
    owner: str | None = None,
    mails_elsewhere: bool = False,
    summary: bool = False,
    sort: str = "value",
    max_results: int = 50,
    year: int | None = None,
) -> str:
    """Look up Prairie Village tax parcels (Johnson County appraisal data) or summarize them.

    One match returns the parcel's full record: use, zoning, year built, grade, lot size,
    appraised and assessed values, and plat/block/lot. Several return a list; summary=True
    returns totals by class, zoning, and subdivision.

    Args:
        address: Address substring, e.g. "4000 W 68TH"
        subdivision: Subdivision or plat name substring, or a 4-digit subdivision code (e.g. "5500")
        block: Block number within the subdivision
        zoning: Zoning district, e.g. "R-1A", "R-1B", "C-2"
        property_class: "Residential", "Commercial", "Exempt", etc.
        use: Land-use substring, e.g. "single family", "duplex", "church"
        tax_unit: County tax unit code
        built_after: Built in or after this year
        built_before: Built in or before this year
        min_value: Minimum appraised value (dollars)
        max_value: Maximum appraised value (dollars)
        corp_or_llc: Only parcels owned by a corporation, LLC, or partnership
        out_of_state: Only parcels whose owner mails from outside Kansas
        mail_state: Owner's two-letter mailing state, e.g. "MO"
        owner: Owner name substring (only when the server runs with PVC_MCP_OWNERS=1)
        mails_elsewhere: Only parcels whose tax bill goes somewhere other than the property (needs PVC_MCP_OWNERS=1)
        summary: Return totals instead of a list
        sort: List order: "value", "assessed", "year", or "address"
        max_results: Maximum parcels listed (default 50)
        year: Valuation year (default: latest imported)
    """
    try:
        pm, records = _parcels(year)
    except FileNotFoundError as e:
        return str(e)
    if (owner or mails_elsewhere) and not pm.has_owners(records):
        return ("Owner data isn't available to this server. It needs the local parcels/<year>-owners.csv "
                "and PVC_MCP_OWNERS=1 in the server's environment.")
    matched = pm.query(records, address=address, owner=owner, zoning=zoning, cls=property_class, use=use,
                       tax_unit=tax_unit, subdivision=subdivision, block=block,
                       built_after=built_after, built_before=built_before,
                       min_value=min_value, max_value=max_value, mails_elsewhere_only=mails_elsewhere,
                       corp_or_llc=corp_or_llc, out_of_state=out_of_state, mail_state=mail_state)
    if not matched:
        return "No matching parcels."
    if summary:
        return pm.render_summary(matched)
    if len(matched) == 1:
        return pm.render_detail(matched[0])
    key = {"value": lambda p: -(p["total_value"] or 0),
           "assessed": lambda p: -(p["assessed_total"] or 0),
           "year": lambda p: -(p["year_built"] or 0),
           "address": lambda p: p["address"] or ""}.get(sort)
    if not key:
        return 'sort must be "value", "assessed", "year", or "address".'
    matched.sort(key=key)
    return (f"{len(matched):,} parcels\n\n"
            + pm.render_list(matched, max_results).replace("(use -n 0 for all)", "(raise max_results for more)"))


# ---------------------------------------------------------------------------
# Subdivisions, plats, and restrictions
# ---------------------------------------------------------------------------

def _map_features() -> list[dict]:
    from pvcouncil import subdivmap

    return mcpassets.load(subdivmap.GEOJSON_PATH, {"features": []})["features"]


def _manifest() -> dict:
    from pvcouncil import plats

    return mcpassets.load(plats.MANIFEST_PATH, {"plats": {}, "instruments": []})


def _resolve_subdivision(q: str) -> tuple[str | None, set[str]]:
    """A subdivision code and its plat names, from a code or a (part of a) plat or subdivision name."""
    names = _manifest()["plats"]
    q_up = q.strip().upper()
    if q_up.isdigit():
        code = q_up.zfill(4)
        return code, {n for n, v in names.items() if v["subdivision_code"] == code}
    exact = [v for n, v in names.items() if q_up in (n, v["subdivision"])]
    hits = exact or [v for n, v in names.items() if q_up in n or q_up in (v["subdivision"] or "")]
    codes = {v["subdivision_code"] for v in hits}
    if len(codes) != 1:
        return None, {f"{v['subdivision']} ({v['subdivision_code']})" for v in hits}
    code = codes.pop()
    return code, {n for n, v in names.items() if v["subdivision_code"] == code}


def _has_image(book_page: str) -> str:
    d = mcpassets.find_doc(book_page)
    return f", {d['pages']} p image" if d else ""


def _decl_line(x: dict) -> str:
    parts = [f"{x['book_page']} ({x['recorded']}) {x['kind']}"]
    if x.get("blocks_label") or x.get("lots_label"):
        parts.append(f"covers {x.get('lots_label') or x.get('blocks_label')}")
    if x.get("term"):
        parts.append(x["term"])
    if x.get("features"):
        parts.append("; ".join(x["features"]))
    if x.get("racial"):
        parts.append("RACIAL COVENANT")
    for k in ("released", "reaffirmed"):
        if x.get(k):
            r = x[k]
            parts.append(f"{k} {r['recorded']} by {r['book_page']}" + (f" ({r['by']})" if r.get("by") else "")
                         + (f" under {r['law']}" if r.get("law") else ""))
    for k in ("expired", "annulled"):
        if x.get(k):
            parts.append(f"{k}: {x[k]}")
    if x.get("note"):
        parts.append(f"note: {x['note']}")
    return " — ".join(parts) + _has_image(x["book_page"])


@mcp.tool()
def subdivisions(name: str | None = None, status: str | None = None) -> str:
    """List Prairie Village subdivisions with their code, restriction status, and parcel count.

    Status (from the restrictions survey): "racial" (a racial covenant still of record), "released"
    (released, e.g. under Kansas HB 2582), "expired", "reference" (carried by reference from
    another declaration), "restrictions" (restrictions but no racial covenant found), "none".

    Args:
        name: Only subdivisions whose name contains this
        status: Only subdivisions (or parts) with this status
    """
    by_code = defaultdict(list)
    for f in _map_features():
        p = f["properties"]
        by_code[p["code"]].append(p)
    counts = defaultdict(int)
    for v in _manifest()["plats"].values():
        counts[v["subdivision_code"]] += v.get("parcels", 0)
    lines = []
    for code, parts in sorted(by_code.items(), key=lambda kv: kv[1][0]["name"]):
        if name and not any(name.upper() in p["name"].upper() for p in parts):
            continue
        if status and not any(p["status"] == status for p in parts):
            continue
        for p in parts:
            lines.append(f"{code}  {p['name']}: {p['status']}"
                         + (f", {counts[code]} parcels" if counts.get(code) else "")
                         + (f" — {p['note']}" if p.get("note") else ""))
    return f"{len(lines)} subdivisions or parts:\n" + "\n".join(lines) if lines else "No matching subdivisions."


@mcp.tool()
def subdivision(name_or_code: str) -> str:
    """Everything recorded about one subdivision's restrictions: its plats, the declarations and
    homes-association instruments filed with them (terms, blocks covered, racial covenants, releases),
    restrictions written on the plat, other indexed restriction instruments, unread Miscellaneous
    candidates, sampled deed chains, and parcel totals. Documents are cited by book and page; pass
    those to document_page_image or document_text.

    Args:
        name_or_code: Subdivision or plat name (e.g. "Prairie Village", "Kenilworth") or 4-digit code (e.g. "5500")
    """
    from pvcouncil import deedchain, platocr, restrictions, restrictread

    code, plat_names = _resolve_subdivision(name_or_code)
    if not code:
        if plat_names:
            return "Ambiguous; matches: " + "; ".join(sorted(plat_names))
        return f"No subdivision matches {name_or_code!r}. Use the subdivisions tool to list them."
    names = _manifest()["plats"]
    sub_name = next((names[n]["subdivision"] for n in plat_names), code)
    out = [f"# {sub_name} (subdivision code {code})", f"Plat names: {', '.join(sorted(plat_names))}"]

    parts = [f["properties"] for f in _map_features() if f["properties"]["code"] == code]
    if parts:
        out += ["", "## Restriction status"]
        for p in parts:
            out.append(f"- {p['name']}: {p['status']}" + (f" — {p['note']}" if p.get("note") else ""))
        out.append(f"Report: {parts[0]['report']}")
        details = json.loads(parts[0]["details"])
        seen = set()
        decls = [x for p in parts for x in json.loads(p["details"])["declarations"]
                 if not (x["book_page"] in seen or seen.add(x["book_page"]))]
        if decls:
            out += ["", "## Declarations and homes-association instruments"] + [f"- {_decl_line(x)}" for x in decls]
        if details.get("on_plat"):
            out += ["", "## Written on the plat"]
            out += [f"- {x['book_page']} ({x['recorded']}): {', '.join(x['terms'])}{_has_image(x['book_page'])}"
                    for x in details["on_plat"]]
        if details.get("other"):
            out += ["", "## Other instruments"]
            out += [f"- {x['book_page']} ({x.get('recorded', '')}) {x.get('kind', '')}"
                    f"{(' — ' + x['note']) if x.get('note') else ''}{_has_image(x['book_page'])}"
                    for x in details["other"]]

    plat_rows = [e for e in _manifest()["instruments"]
                 if e["match"] in ("exact", "variant") and e.get("subdivision_code") == code]
    if plat_rows:
        out += ["", "## Recorded plats"]
        out += [f"- {e['book_page']} ({e['recorded']}) {e['plat']}{_has_image(e['book_page'])}"
                for e in sorted(plat_rows, key=lambda e: e["recorded"])]

    on_plat = [p for p in mcpassets.load(platocr.RESULTS_PATH, {"plats": []})["plats"] if p["subdivision_code"] == code]
    cites = [f"MISC {c['book']} {c['page']}" + (" (uncertain)" if c.get("uncertain") else "")
             for p in on_plat for c in p["citations"]]
    if cites:
        out += ["", "## Restrictions the plats cite", "- " + "; ".join(cites)]

    reads = [r for r in mcpassets.load(restrictread.RESULTS_PATH, {"read": []})["read"]
             if set(r["plats"]) & plat_names and not r.get("unrelated")]
    if reads:
        out += ["", "## Instruments filed with the plats (OCR-classified)"]
        out += [f"- {r['book_page']} ({r['recorded']}) {r['kind']}"
                f"{(' — ' + '; '.join(r['features'])) if r['features'] else ''}"
                f"{(' — ' + r['term']) if r.get('term') else ''}{(' — note: ' + r['note']) if r.get('note') else ''}"
                f"{_has_image(r['book_page'])}" for r in reads]

    rs = mcpassets.load(restrictions.RESULTS_PATH, {})
    indexed = [e for e in rs.get("instruments", []) if code in (e.get("subdivision_codes") or [])]
    if indexed:
        out += ["", f"## Indexed restriction instruments ({len(indexed)})"]
        out += [f"- {e['book_page']} ({e['recorded']}) {e['doc_type']}{_has_image(e['book_page'])}"
                for e in sorted(indexed, key=lambda e: e["recorded"])[:40]]
        if len(indexed) > 40:
            out.append(f"- … {len(indexed) - 40} more (restriction_documents lists them all)")
    cands = [c for c in rs.get("misc_candidates", []) if set(c["plats"]) & plat_names]
    if cands:
        out += ["", "## Unread Miscellaneous filings that may be restrictions"]
        out += [f"- {c['book_page']} ({c['recorded']}) score {c['score']}: {', '.join(c['why'])}{_has_image(c['book_page'])}"
                for c in sorted(cands, key=lambda c: -c["score"])[:15]]

    chain = mcpassets.load(deedchain.RESULTS_PATH, {"deeds": []})["deeds"]
    lots = defaultdict(list)
    for d in chain:
        if d["subdivision"] in plat_names or d["subdivision"] == sub_name:
            lots[(d["block"], d["lot"])].append(d)
    if lots:
        out += ["", "## Sampled deed chains"]
        for (b, lot), ds in sorted(lots.items()):
            racial = sum(d["racial"] for d in ds)
            out.append(f"- Block {b} Lot {lot or '—'}: {len(ds)} deeds {ds[0]['recorded'][:4]}–{ds[-1]['recorded'][:4]}, "
                       f"{racial} with racial language, {sum(bool(d.get('restates_restrictions')) for d in ds)} restate restrictions")

    try:
        from pvcouncil import parcels as pm

        _, records = _parcels(None)
        row = next((r for r in pm.subdivision_rows(records) if r["code"] == code), None)
        if row:
            out += ["", "## Parcels",
                    f"{row['parcels']:,} parcels, {row['homes']:,} single-family homes, assessed {pm.money(row['assessed'])}"
                    + (f", median home {pm.money(row['median_home'])}" if row["median_home"] else "")
                    + (f", median year built {row['median_year']}" if row["median_year"] else "")]
    except FileNotFoundError:
        pass
    return "\n".join(out)


def _in_range(rec: dict, date_from: str | None, date_to: str | None) -> bool:
    return (not date_from or rec["recorded"] >= date_from) and (not date_to or rec["recorded"] <= date_to)


@mcp.tool()
def restriction_documents(
    subdivision: str | None = None,
    kind: str | None = None,
    feature: str | None = None,
    racial_only: bool = False,
    source: str = "read",
    date_from: str | None = None,
    date_to: str | None = None,
    max_results: int = 100,
) -> str:
    """List recorded restriction instruments across Prairie Village.

    Args:
        subdivision: Only instruments tied to this subdivision (name or code)
        kind: Only this kind: "restrictions", "homes association", "restriction amendment", "release", "easement", "deed", "affidavit", "other"
        feature: Only instruments with this term, e.g. "residence only", "building line / setback", "minimum cost", "architectural approval", "racial restriction"
        racial_only: Only instruments with a racially restrictive covenant
        source: "read" (instruments filed with plats and pre-plat instruments, OCR-classified; default), "indexed" (the recorder's Declaration of Restrictions / Homes Association index), "candidates" (unread Miscellaneous filings scored as likely restrictions), or "all"
        date_from: Recorded on or after (YYYY-MM-DD)
        date_to: Recorded on or before (YYYY-MM-DD)
        max_results: Maximum rows (default 100)
    """
    from pvcouncil import preplat, restrictions, restrictread

    code, plat_names = (None, set())
    if subdivision:
        code, plat_names = _resolve_subdivision(subdivision)
        if not code:
            return "Ambiguous or unknown subdivision: " + ("; ".join(sorted(plat_names)) or subdivision)
    if source not in ("read", "indexed", "candidates", "all"):
        return 'source must be "read", "indexed", "candidates", or "all".'
    rows = []
    if source in ("read", "all"):
        for r in mcpassets.load(restrictread.RESULTS_PATH, {"read": []})["read"]:
            if r.get("unrelated") or (code and not set(r["plats"]) & plat_names):
                continue
            rows.append({**r, "src": "filed with plat", "racial": "racial restriction" in r["features"],
                         "where": ", ".join(r["plats"])})
        if not code:
            for r in mcpassets.load(preplat.RESULTS_PATH, {"instruments": []})["instruments"]:
                rows.append({**r, "src": "pre-plat", "where": f"acreage, later {r.get('later_plat') or '?'}"})
    if source in ("indexed", "all", "candidates"):
        rs = mcpassets.load(restrictions.RESULTS_PATH, {})
        if source != "candidates":
            for e in rs.get("instruments", []):
                if code and code not in (e.get("subdivision_codes") or []):
                    continue
                rows.append({**e, "src": "indexed", "kind": e["doc_type"], "features": [], "racial": None,
                             "where": ", ".join(e["plats"])})
        if source != "indexed":
            for c in rs.get("misc_candidates", []):
                if code and not set(c["plats"]) & plat_names:
                    continue
                rows.append({**c, "src": f"candidate (score {c['score']})", "kind": "unread", "features": [],
                             "racial": None, "where": ", ".join(c["plats"] or c.get("nearby_plats") or [])})
    rows = [r for r in rows if _in_range(r, date_from, date_to)
            and (not kind or r["kind"] == kind)
            and (not feature or feature in r["features"])
            and (not racial_only or r["racial"])]
    if not rows:
        return "No matching instruments."
    rows.sort(key=lambda r: r["recorded"])
    lines = [f"{len(rows)} instruments (showing {min(len(rows), max_results)}):"]
    for r in rows[:max_results]:
        lines.append(f"{r['book_page']} ({r['recorded']}) {r['kind']} [{r['src']}] — {r['where']}"
                     + (f" — {'; '.join(r['features'])}" if r["features"] else "")
                     + (f" — {r['term']}" if r.get("term") else "")
                     + (f" — note: {r['note']}" if r.get("note") else "")
                     + _has_image(r["book_page"]))
    return "\n".join(lines)


@mcp.tool()
def preplat_covenants(racial_only: bool = True) -> str:
    """Covenants put on acreage before it was platted (J. C. Nichols often had owners restrict land
    before he bought and platted it). These are indexed to sections of land, not subdivisions, so
    the subdivision tool doesn't show them.

    Args:
        racial_only: Only the racial covenants read by eye, with the land they cover (default). False lists every pre-plat instrument read.
    """
    from pvcouncil import preplat, subdivmap

    if racial_only:
        feats = mcpassets.load(subdivmap.MAP_DIR / "preplat.geojson", {"features": []})["features"]
        seen, lines = set(), []
        for f in feats:
            p = f["properties"]
            if p["book_page"] in seen:
                continue
            seen.add(p["book_page"])
            lines.append(f"{p['book_page']} ({p['recorded']}) by {p['by']}; {p['term']}, ended {p['ended']}; "
                         f"land later platted as {p.get('later_plat') or '?'} ({p.get('first_plat') or '?'}). "
                         f"{p['summary']}{_has_image(p['book_page'])}")
        return "\n\n".join(lines) or "No pre-plat covenants mapped."
    rows = mcpassets.load(preplat.RESULTS_PATH, {"instruments": []})["instruments"]
    return f"{len(rows)} pre-plat instruments:\n" + "\n".join(
        f"{r['book_page']} ({r['recorded']}) {r['kind']}{' RACIAL' if r.get('racial') else ''}"
        f"{' (Nichols)' if r.get('nichols') else ''} — later {r.get('later_plat')}{_has_image(r['book_page'])}"
        for r in rows)


@mcp.tool()
def deed_chain(subdivision: str | None = None, block: str | None = None, lot: str | None = None,
               racial_only: bool = False) -> str:
    """Sampled lots' deeds from the developer's first sale to today, read for racial language written
    into the deed itself and for how each deed treats the subdivision's restrictions (restated in
    full, or only cited as "subject to restrictions of record").

    Args:
        subdivision: Subdivision name substring (e.g. "PRAIRIE VILLAGE")
        block: Block number
        lot: Lot number
        racial_only: Only deeds with racial language
    """
    from pvcouncil import deedchain

    data = mcpassets.load(deedchain.RESULTS_PATH, {"deeds": [], "lots": []})
    ds = [d for d in data["deeds"]
          if (not subdivision or subdivision.upper() in d["subdivision"])
          and (not block or d["block"] == block) and (not lot or d["lot"] == lot)
          and (not racial_only or d["racial"])]
    if not ds:
        lots = "; ".join(f"{x['subdivision']} {x['block']}/{x['lot']}" for x in data["lots"])
        return f"No matching deeds. Sampled lots (subdivision block/lot): {lots}"
    lines = [f"{len(ds)} deeds:"]
    for d in ds:
        flags = [k.replace("_", " ") for k in ("racial", "cites_restrictions", "restates_restrictions", "approval_clause") if d.get(k)]
        if d.get("minimum_cost"):
            flags.append(f"minimum cost ${d['minimum_cost']:,}")
        lines.append(f"{d['subdivision']} B{d['block']} L{d['lot'] or '—'}: {d['book_page']} ({d['recorded']}) {d['type']}, "
                     f"{d['pages']} p, read by {d.get('read_by') or '—'} — {', '.join(flags) or 'no restriction language'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Recorded documents: what's known, text, and page images
# ---------------------------------------------------------------------------

def _references(book_page: str) -> list[str]:
    """Every dataset's record of one recorded document."""
    from pvcouncil import deedchain, platocr, preplat, restrictions, restrictread

    bp = mcpassets.norm_book_page(book_page)
    refs = []
    for e in _manifest()["instruments"]:
        if e["book_page"] == bp:
            refs.append(f"Plat survey: {e['doc_type']} recorded {e['recorded']}, plat {e['plat']}, "
                        f"match {e['match']}, subdivision {e.get('subdivision_code') or '—'}")
    for p in mcpassets.load(platocr.RESULTS_PATH, {"plats": []})["plats"]:
        if p["book_page"] == bp:
            refs.append(f"Plat OCR: restriction terms {', '.join(p['terms']) or 'none'}; cites "
                        + (", ".join(f"MISC {c['book']} {c['page']}" for c in p["citations"]) or "nothing"))
    for r in mcpassets.load(restrictread.RESULTS_PATH, {"read": []})["read"]:
        if r["book_page"] == bp:
            refs.append(f"Filed with plat {', '.join(r['plat_book_pages'])} ({', '.join(r['plats'])}): {r['kind']}; "
                        f"{'; '.join(r['features']) or 'no terms found'}; term {r.get('term') or '—'}"
                        + (f"; reviewed by eye: {r['note']}" if r.get("reviewed") else "")
                        + ("; concerns other land" if r.get("unrelated") else ""))
    rs = mcpassets.load(restrictions.RESULTS_PATH, {})
    for e in rs.get("instruments", []):
        if e["book_page"] == bp:
            refs.append(f"Recorder index: {e['doc_type']} recorded {e['recorded']}, matched to {', '.join(e['plats'])} "
                        f"by {', '.join(e['matched_by'])}")
    for c in rs.get("misc_candidates", []):
        if c["book_page"] == bp:
            refs.append(f"Miscellaneous walk candidate, score {c['score']}: {', '.join(c['why'])}")
    for r in mcpassets.load(preplat.RESULTS_PATH, {"instruments": []})["instruments"]:
        if r["book_page"] == bp:
            refs.append(f"Pre-plat instrument: {r['kind']}{', racial' if r.get('racial') else ''}, "
                        f"land later platted as {r.get('later_plat')} ({r.get('first_plat')})")
    for d in mcpassets.load(deedchain.RESULTS_PATH, {"deeds": []})["deeds"]:
        if d["book_page"] == bp:
            refs.append(f"Deed chain: {d['subdivision']} block {d['block']} lot {d['lot']}, "
                        f"racial {d['racial']}, restates restrictions {d.get('restates_restrictions')}")
    for f in _map_features():
        for x in json.loads(f["properties"]["details"])["declarations"]:
            if bp in (x["book_page"], (x.get("released") or {}).get("book_page"), (x.get("reaffirmed") or {}).get("book_page")):
                refs.append(f"Map, {f['properties']['name']} ({f['properties']['status']}): {_decl_line(x)}")
    return list(dict.fromkeys(refs))


@mcp.tool()
def document_info(book_page: str) -> str:
    """What every dataset records about one recorded document (plat, declaration, deed, release),
    and whether its image is available for document_page_image and document_text.

    Args:
        book_page: The recorder's book and page, e.g. "MISC 26 349", "PLAT 12 41", "OR 200711 001865"
    """
    bp = mcpassets.norm_book_page(book_page)
    doc = mcpassets.find_doc(bp)
    lines = [bp]
    if doc:
        hl = mcpassets.highlights(bp)
        lines.append(f"Image: {doc['kind']}, {doc['pages']} page(s)"
                     + (f"; racial language outlined on page(s) {', '.join(map(str, sorted(hl)))}" if hl else ""))
    else:
        lines.append("Image: not downloaded")
    refs = _references(bp)
    lines += [f"- {r}" for r in refs] if refs else ["No dataset references this document."]
    return "\n".join(lines)


def _ocr_text(doc: dict, engine: str | None) -> tuple[str, str]:
    from pvcouncil import ocrengines, platocr, plats, transcripts

    rel = doc["path"].parent.relative_to(plats.IMAGES_DIR)
    txt = platocr.OCR_DIR / rel / f"{doc['path'].stem}.txt"
    tess = txt.read_text() if txt.exists() else ""
    if not doc["key"]:
        return tess, "Tesseract"
    if engine:
        r = ocrengines.load_reading(engine, doc["key"])
        if not r:
            have = ", ".join(ocrengines.readings_for(doc["key"])) or "none"
            raise ValueError(f"No {engine} reading of this document (readings: {have}).")
        text, label = ocrengines.reading_text(r), r.get("label", engine)
    else:
        text, chosen = ocrengines.base_text(doc["key"], tess, doc["path"])
        label = chosen["label"] if chosen else "Tesseract"
        # the deed chain reads deeds with olmOCR whether or not a reading was adopted (deedchain.text_for)
        if not chosen and doc["kind"] == "deed" and (r := ocrengines.load_reading("olmocr", doc["key"])):
            text, label = ocrengines.reading_text(r), "olmOCR-2"
    extra = transcripts.text_for(doc["key"])
    if extra:
        text += "\n\n" + extra
        label += " + hand transcriptions"
    return text, label


@mcp.tool()
def document_text(book_page: str, pages: str | None = None, engine: str | None = None,
                  max_chars: int = 40000) -> str:
    """OCR text of a recorded document. Uses the reading adopted for the report when there is one
    (a vision OCR engine's, for poor scans), else Tesseract's, plus any hand transcriptions. Old
    scans and photostat negatives OCR badly; check important wording against document_page_image.

    Args:
        book_page: The recorder's book and page, e.g. "MISC 26 349"
        pages: Optional page or range, e.g. "2" or "1-3" (instruments and deeds; plats are one sheet)
        engine: Force one engine's reading: "olmocr", "surya", "qwen-vl", "gemma4"
        max_chars: Truncate after this many characters (default 40000)
    """
    bp = mcpassets.norm_book_page(book_page)
    doc = mcpassets.find_doc(bp)
    if not doc:
        return f"No image of {bp} is downloaded, so there's no OCR text."
    try:
        text, label = _ocr_text(doc, engine)
    except ValueError as e:
        return str(e)
    if not text.strip():
        return f"{bp} has an image but no OCR text yet."
    if pages:
        a, _, b = pages.partition("-")
        want = set(range(int(a), int(b or a) + 1))
        chunks = re.split(r"(?=\[(?:transcribed, )?page \d+\])", text)
        text = "".join(c for c in chunks if (m := re.match(r"\[(?:transcribed, )?page (\d+)\]", c)) and int(m.group(1)) in want)
    head = f"{bp} — {doc['kind']}, {doc['pages']} page(s), text from {label}\n\n"
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[… truncated; {len(text) - max_chars:,} more characters]"
    return head + text


@mcp.tool()
def document_page_image(book_page: str, page: int = 1, region: list[float] | None = None,
                        rotate: int = 0, invert: bool = False, highlight: bool = True,
                        max_side: int = mcpassets.DEFAULT_SIDE) -> list:
    """Image of one page of a recorded document: a plat, declaration of restrictions, homes
    association declaration, release, or deed.

    Plat sheets are large: read the whole sheet first, then pass a region to zoom into lettering,
    a dedication, or a lot. Many 1950s instruments are photostat negatives (white on black); pass
    invert=True to read them dark on light. Racial-covenant language found by the highlights survey
    is outlined in red unless highlight=False.

    Args:
        book_page: The recorder's book and page, e.g. "MISC 26 349", "PLAT 12 41"
        page: Page number (1-based)
        region: Optional [x0, y0, x1, y1] as fractions of the unrotated page (0–1), e.g. [0.5, 0, 1, 0.5] for the top-right quarter
        rotate: Rotate clockwise by 0, 90, 180, or 270 degrees (some plats print their dedication sideways)
        invert: Invert black and white
        highlight: Outline racial-covenant language where it has been located
        max_side: Longest side of the image in pixels (default 1568, max 2400)
    """
    bp = mcpassets.norm_book_page(book_page)
    doc = mcpassets.find_doc(bp)
    if not doc:
        return [f"No image of {bp} is downloaded. document_info lists what's known about it."]
    boxes = mcpassets.highlights(bp).get(page) if highlight else None
    try:
        data, info = mcpassets.render(doc["path"], page, region=region, rotate=rotate, invert=invert,
                                      boxes=boxes, max_side=max_side)
    except ValueError as e:
        return [str(e)]
    note = (f"{bp} ({doc['kind']}), page {info['page']} of {info['pages']}"
            + (f", region {region}" if region else "")
            + f", {info['width_px']}×{info['height_px']} px"
            + (f" ({info['of_scan']:.0%} of the scan's resolution)" if info["of_scan"] else "")
            + (f"; {len(boxes)} racial-language box(es) outlined in red" if boxes else ""))
    # typed instruments read fine at a fifth of their scan resolution; plat lettering needs more
    if info["of_scan"] and info["of_scan"] < (0.5 if doc["kind"] == "plat" else 0.2):
        note += "; fine print may not be legible at this size, so pass a region to zoom in"
    return [note, Image(data=data, format="jpeg")]


@mcp.tool()
def subdivision_map(subdivision: str | None = None, show_parcels: bool = True) -> list:
    """A map image of Prairie Village's subdivisions, colored by restriction status (red: racial
    covenant of record; green: released; gray: expired; orange: carried by reference; blue:
    restrictions without a racial covenant; light gray: none found). With a subdivision, zooms to
    it and draws its parcels.

    Args:
        subdivision: Optional subdivision name or code to zoom to
        show_parcels: When zoomed to a subdivision, draw its parcel outlines (default true)
    """
    from pvcouncil import parcelmap

    feats = _map_features()
    if not feats:
        return ["No map/subdivisions.geojson; run `pvc subdivision-map`."]
    focus, parcels_geo = None, None
    if subdivision:
        code, names = _resolve_subdivision(subdivision)
        if not code:
            return ["Ambiguous or unknown subdivision: " + ("; ".join(sorted(names)) or subdivision)]
        focus = {i for i, f in enumerate(feats) if f["properties"]["code"] == code}
        if not focus:
            return [f"Subdivision {code} isn't on the map."]
        if show_parcels and parcelmap.GEOJSON_PATH.exists():
            parcels_geo = [p for p in mcpassets.load(parcelmap.GEOJSON_PATH, {"features": []})["features"]
                           if (p["properties"].get("id") or "").startswith(f"OP{code}")]
    png = mcpassets.subdivision_map(feats, focus, parcels_geo)
    caption = (f"Subdivision {subdivision}: " + "; ".join(f"{feats[i]['properties']['name']} ({feats[i]['properties']['status']})"
                                                          for i in sorted(focus))) if focus else f"{len(feats)} subdivisions and parts"
    return [caption, Image(data=png, format="png")]


# ---------------------------------------------------------------------------
# Resources: the checked-in reports, the map data, and page images by URI
# ---------------------------------------------------------------------------

REPORTS = {
    "subdivision-restrictions": "Restrictions and covenants found for each subdivision",
    "subdivision-plats": "Each subdivision's recorded plats",
    "property-2026": "2026 property values by class, zoning, subdivision, and housing type",
    "budget-history": "City budget headline figures by year",
    "council-votes": "Every recorded council vote since April 2015",
    "pv-municipal-complex-history": "History of the municipal complex project",
}


def _add_report(name: str, description: str) -> None:
    path = config.PROJECT_ROOT / f"{name}.md"

    @mcp.resource(f"pv://reports/{name}", name=name, description=description, mime_type="text/markdown")
    def report() -> str:
        return path.read_text()


for _name, _desc in REPORTS.items():
    if (config.PROJECT_ROOT / f"{_name}.md").exists():
        _add_report(_name, _desc)


@mcp.resource("pv://map/subdivisions.geojson", name="subdivisions.geojson", mime_type="application/geo+json",
              description="Subdivision outlines with restriction status and declarations (GeoJSON)")
def subdivisions_geojson() -> str:
    from pvcouncil import subdivmap

    return subdivmap.GEOJSON_PATH.read_text()


@mcp.resource("pv://documents/{slug}/{page}", mime_type="image/jpeg",
              description="A page of a recorded document by slug ('misc-26-349') and page number")
def document_page(slug: str, page: str) -> bytes:
    doc = mcpassets.find_doc(slug.replace("-", " "))
    if not doc:
        raise ValueError(f"No image for {slug}")
    return mcpassets.render(doc["path"], int(page))[0]


def run(http: bool = False, host: str = "127.0.0.1", port: int = 8000) -> None:
    if http:
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run("streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    run()
