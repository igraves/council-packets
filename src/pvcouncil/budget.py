"""Extract annual budget figures from council packets via Claude structured outputs.

Each year's budget moves through the packets in stages: the preliminary
budget presentation (May/June), the published notice and revenue-neutral-rate
forms (July/August), and the adopted budget (August/September). Packets from
May through September of year Y are scanned for budget pages about year Y+1;
only those pages are sent to Claude, which returns headline figures, fund and
expense-category totals, department totals, and the cost drivers staff cite.

The published budget books in the city's Archive Center (2006 onward, see
archive.py) go through the same extraction. Each book names its budget year
in its title, and an adopted book outranks the packets for its year.

Results are stored per document in budgets.json (same size+mtime incremental
tracking as votes.json). budget_years() merges them into one record per
budget year, preferring adopted over proposed over preliminary figures.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import anthropic

from pvcouncil.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, PROJECT_ROOT
from pvcouncil.extract import extract_pages
from pvcouncil.index import discover_docs, _file_hash

BUDGETS_PATH = PROJECT_ROOT / "budgets.json"

# Budgets for year Y+1 are prepared and adopted in these months of year Y.
BUDGET_MONTHS = {"05", "06", "07", "08", "09"}
# Archive Center budget books ("2026 Budget", "2027 Preliminary Budget Book
# ..."). "Budget in Brief" summaries duplicate the full book for their year.
_BUDGET_BOOK = re.compile(r"^\d{4}-\d{2}-\d{2}_((?:19|20)\d{2}) .*Budget(?! in Brief)", re.I)
MIN_PAGES = 3    # fewer selected pages than this = a passing mention, not a budget item
MAX_PAGES = 80   # cap per packet; keeps one request well inside the context window

_MARKERS = [re.compile(p, re.I) for p in (
    r"ad valorem", r"revenue neutral", r"mill levy", r"budget authority",
    r"personnel services", r"contract(ual)? services", r"commodities",
    r"capital outlay", r"fund balance", r"decision package", r"general fund",
    r"expenditures", r"assessed valuation", r"debt service",
    r"transfers? (in|out|to)", r"budget (summary|overview|calendar|presentation|hearing)",
    r"cost of living|merit", r"health insurance",
)]
_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+")
# Claims ordinances and check registers are dense with dollar amounts but
# carry no budget information.
_NOISE = re.compile(r"claims ordinance|check date|ap checks", re.I)

STAGE_RANK = {"adopted": 3, "proposed": 2, "preliminary": 1, "none": 0}

HEADLINE_METRICS = {
    "total_budget_authority": "Total budget authority, all budgeted funds ($)",
    "total_expenditures": "Total expenditures, all funds ($)",
    "general_fund_expenditures": "General Fund expenditures including transfers out ($)",
    "ad_valorem_tax_levied": "Property (ad valorem) tax levied ($)",
    "mill_levy": "Total city mill levy (mills)",
    "revenue_neutral_rate": "Revenue-neutral rate (mills); Kansas introduced it in 2021",
    "assessed_valuation": "Assessed valuation ($)",
    "general_fund_ending_balance": "Projected General Fund ending balance ($)",
    "general_fund_reserve_pct": "General Fund ending balance as a percent of expenditures (e.g. 25.5)",
    "full_time_equivalents": "Budgeted staff positions, full-time equivalents",
}

# Every figure is a row tagged with the year it belongs to, so a packet's
# prior-year columns are captured alongside the budget year and a figure the
# packet doesn't state is simply absent. (Structured outputs also limit how
# many nullable fields a schema may have, which rules out a fixed grid of
# nullable metrics.)
_PAGE = {"type": "integer",
         "description": "Packet page number from the nearest preceding === Page N === marker"}
_YEAR = {"type": "integer", "description": "Fiscal year the figure belongs to"}
_BASIS = {"type": "string", "enum": ["budget", "estimate", "actual"],
          "description": "budget = adopted or proposed budget figure; estimate = current-year estimate; actual = audited or year-end actual"}


def _rows(props: dict, description: str) -> dict:
    return {
        "type": "array",
        "description": description,
        "items": {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    }


BUDGET_SCHEMA = {
    "type": "object",
    "properties": {
        "budget_year": {
            "type": "integer",
            "description": "The fiscal year being budgeted (e.g. 2027 for a budget adopted in 2026); 0 if these pages hold no budget",
        },
        "stage": {
            "type": "string",
            "enum": ["preliminary", "proposed", "adopted", "none"],
            "description": (
                "preliminary = staff or finance committee draft presented for council review (usually May-June); "
                "proposed = notice of the budget hearing or of intent to exceed the revenue-neutral rate, "
                "published ahead of the hearing (usually July); "
                "adopted = the packet for the budget hearing and adoption vote, or the certified budget "
                "(usually August-September); none = no budget content"
            ),
        },
        "headline": _rows({
            "metric": {"type": "string", "enum": list(HEADLINE_METRICS),
                       "description": "; ".join(f"{k}: {d}" for k, d in HEADLINE_METRICS.items())},
            "year": _YEAR, "basis": _BASIS,
            "value": {"type": "number"},
            "page": _PAGE,
        }, "Citywide headline figures, one row per metric per year shown"),
        "funds": _rows({
            "fund": {"type": "string", "description": "Fund name as printed, e.g. 'General Fund', 'Solid Waste Fund'"},
            "year": _YEAR, "basis": _BASIS,
            "expenditures": {"type": "number", "description": "Expenditures or budget authority for the fund ($)"},
            "page": _PAGE,
        }, "Fund totals, one row per fund per year shown"),
        "expense_categories": _rows({
            "label": {"type": "string", "description": "General Fund expense category as printed"},
            "category": {"type": "string",
                         "enum": ["personnel", "contract_services", "commodities", "capital_outlay",
                                  "debt_service", "transfers", "contingency", "other"]},
            "year": _YEAR, "basis": _BASIS,
            "amount": {"type": "number"},
            "page": _PAGE,
        }, "General Fund spending by type of expense, one row per category per year shown"),
        "departments": _rows({
            "department": {"type": "string", "description": "Department or program as printed, e.g. 'Police', 'Public Works'"},
            "year": _YEAR, "basis": _BASIS,
            "amount": {"type": "number"},
            "page": _PAGE,
        }, "General Fund spending by department, one row per department per year shown"),
        "drivers": _rows({
            "description": {"type": "string", "description": "One sentence: what changed and why, as the documents explain it"},
            "category": {"type": "string",
                         "enum": ["personnel", "health_insurance", "public_safety", "capital",
                                  "debt_service", "contract_services", "solid_waste",
                                  "revenue", "tax_rate", "reserves", "other"]},
            "direction": {"type": "string", "enum": ["increase", "decrease"]},
            "amount_change": {"anyOf": [{"type": "number"}, {"type": "null"}],
                              "description": "Dollar change attributed to this driver; null if not stated"},
            "page": _PAGE,
        }, "Reasons the documents give for changes from the prior year's budget"),
        "assumptions": _rows({
            "item": {"type": "string"},
            "value": {"type": "string", "description": "As printed, e.g. '4%' or '$22.88/month'"},
            "page": _PAGE,
        }, "Rates and assumptions the budget rests on, e.g. merit pool, COLA, health insurance increase, sales tax growth"),
        "narrative": {
            "type": "string",
            "description": "Two to four sentences: how staff and council framed this budget and its main changes",
        },
    },
    "required": ["budget_year", "stage", "headline", "funds", "expense_categories",
                 "departments", "drivers", "assumptions", "narrative"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You extract municipal budget data for Prairie Village, Kansas from "
    "pages of city council meeting packets. The pages were preselected by "
    "keyword and may include unrelated material; ignore anything that is not "
    "about the city's annual operating budget. Rules:\n"
    "- Report only figures printed in the pages. Never compute, estimate, or "
    "carry figures over from general knowledge; leave out any figure that is "
    "not stated.\n"
    "- Tag every figure with the fiscal year it belongs to and its basis. "
    "Record only the budget year and the year before it; tables often show "
    "the prior year's budget, estimate, or actual next to the budget year, "
    "and each of those columns is its own row. Skip older years.\n"
    "- Dollar amounts are plain numbers (12986118, not '$12.99M'). When a "
    "table states amounts in thousands, convert to dollars.\n"
    "- Cite the page number from the nearest preceding === Page N === marker.\n"
    "- Drivers are the reasons the documents give for year-over-year changes: "
    "salary and benefit increases, new positions, contracts, capital "
    "projects, debt, lost or new revenue, tax-rate decisions. Keep each one "
    "to what the documents say. The overall total change is not a driver; "
    "list what caused it.\n"
    "- If the pages discuss several budget years, budget_year and stage "
    "describe the one being presented, proposed, or adopted."
)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def select_budget_pages(pages: list[tuple[int, str]], budget_year: int) -> list[tuple[int, str]]:
    """Pick the pages of one packet that discuss the given budget year.

    Strong pages name the year, hit several budget terms, and carry dollar
    tables. Neighbouring pages that name the year and hit a couple of terms
    are kept too, since staff memos explaining the tables sit next to them.
    """
    year = str(budget_year)
    # Some budget books separate words with non-breaking spaces.
    pages = [(n, t.replace("\xa0", " ")) for n, t in pages]
    scored = {}
    for num, text in pages:
        if year not in text or _NOISE.search(text):
            continue
        hits = sum(1 for r in _MARKERS if r.search(text))
        amounts = len(_AMOUNT.findall(text))
        scored[num] = (hits, amounts)

    strong = {n for n, (hits, amounts) in scored.items() if hits >= 3 and amounts >= 5}
    keep = set(strong)
    for n, (hits, _) in scored.items():
        if hits >= 2 and any(abs(n - s) <= 2 for s in strong):
            keep.add(n)

    if len(keep) > MAX_PAGES:
        keep = set(sorted(keep, key=lambda n: scored[n], reverse=True)[:MAX_PAGES])
    return [(n, t) for n, t in pages if n in keep]


def _request_params(pages: list[tuple[int, str]], budget_year: int,
                    source: str = "packet") -> dict:
    """Messages API params for one document — shared by direct and batch paths."""
    origin = (f"the city's published {budget_year} budget book" if source == "archive"
              else f"a council packet that likely covers the {budget_year} budget")
    text = "\n\n".join(f"=== Page {n} ===\n{t}" for n, t in pages)
    return {
        "model": CLAUDE_MODEL,
        # Large multi-column budget tables plus adaptive thinking need room;
        # the direct path streams so this doesn't hit HTTP timeouts.
        "max_tokens": 64000,
        "system": SYSTEM_PROMPT,
        "output_config": {"format": {"type": "json_schema", "schema": BUDGET_SCHEMA}},
        "messages": [{
            "role": "user",
            "content": (
                f"These pages come from {origin}. Extract the budget data:\n\n{text}"
            ),
        }],
    }


def _parse_response(response) -> dict:
    if response.stop_reason == "refusal":
        raise RuntimeError("model refused extraction")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("output truncated at max_tokens")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def load_store() -> dict:
    if BUDGETS_PATH.exists():
        return json.loads(BUDGETS_PATH.read_text())
    return {"files": {}}


def save_store(store: dict) -> None:
    BUDGETS_PATH.write_text(json.dumps(store, indent=1))


def _budget_docs(sources: set[str]) -> list[tuple[dict, int, str]]:
    """Documents that carry a budget: [(doc, budget_year, source)].

    A packet from May–September of year Y covers the Y+1 budget. An archive
    budget book names its budget year in its title.
    """
    out = []
    for d in discover_docs():
        if ("packet" in sources and d["doc_type"] == "packet"
                and d["meeting_date"][5:7] in BUDGET_MONTHS):
            out.append((d, int(d["meeting_date"][:4]) + 1, "packet"))
        elif "archive" in sources and d["doc_type"] == "archive":
            m = _BUDGET_BOOK.match(d["filename"])
            if m:
                out.append((d, int(m.group(1)), "archive"))
    return out


def _pending(force: bool, limit: int | None,
             sources: set[str] = frozenset({"packet", "archive"})) -> tuple[list, int, int]:
    """Budget documents not yet extracted. Returns (todo, total, skipped).

    Each todo item is (doc, hash, budget_year, source, pages). Documents whose
    selection is too thin are recorded as skipped-with-no-pages so they are
    not rescanned on every run.
    """
    store = load_store()
    docs = _budget_docs(sources)
    todo, skipped = [], 0
    for doc, budget_year, source in docs:
        h = _file_hash(doc["path"])
        entry = store["files"].get(doc["filename"])
        if entry and entry.get("hash") == h and not force:
            skipped += 1
            continue
        pages = select_budget_pages(extract_pages(doc["path"]), budget_year)
        if len(pages) < MIN_PAGES:
            store["files"][doc["filename"]] = {
                "hash": h, "meeting_date": doc["meeting_date"], "source": source,
                "expected_year": budget_year,
                "pages": [n for n, _ in pages], "stage": "none",
            }
            skipped += 1
            continue
        todo.append((doc, h, budget_year, source, pages))
        if limit and len(todo) >= limit:
            break
    save_store(store)
    return todo, len(docs), skipped


def _record(doc: dict, h: str, budget_year: int, source: str,
            pages: list, result: dict) -> dict:
    return {"hash": h, "meeting_date": doc["meeting_date"], "source": source,
            "expected_year": budget_year, "pages": [n for n, _ in pages], **result}


def extract_all(force: bool = False, limit: int | None = None,
                workers: int = 4, sources: set[str] = frozenset({"packet", "archive"}),
                progress_fn=None) -> dict:
    """Extract budget data from all budget documents not yet processed.

    Returns stats: {total, extracted, skipped, errors}
    """
    todo, total, skipped = _pending(force, limit, sources)
    stats = {"total": total, "extracted": 0, "skipped": skipped, "errors": 0}
    if not todo:
        return stats

    store = load_store()
    client = _client()

    def work(doc, h, budget_year, source, pages):
        with client.messages.stream(**_request_params(pages, budget_year, source)) as stream:
            resp = stream.get_final_message()
        return doc, h, budget_year, source, pages, _parse_response(resp)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, *item): item[0] for item in todo}
        for fut in as_completed(futures):
            doc = futures[fut]
            done += 1
            try:
                doc, h, budget_year, source, pages, result = fut.result()
                store["files"][doc["filename"]] = _record(doc, h, budget_year, source,
                                                          pages, result)
                stats["extracted"] += 1
                save_store(store)  # checkpoint so an interrupt loses little
                if progress_fn:
                    progress_fn(done, len(todo), doc["filename"],
                                f"{result['stage']} {result['budget_year']} ({len(pages)} pages)")
            except Exception as e:
                stats["errors"] += 1
                if progress_fn:
                    progress_fn(done, len(todo), doc["filename"], f"ERROR: {e}")

    return stats


def extract_all_batch(force: bool = False, limit: int | None = None,
                      poll_seconds: int = 30,
                      sources: set[str] = frozenset({"packet", "archive"}),
                      progress_fn=None) -> dict:
    """Extract budget data via the Message Batches API (50% token cost, async)."""
    import time

    todo, total, skipped = _pending(force, limit, sources)
    stats = {"total": total, "extracted": 0, "skipped": skipped, "errors": 0}
    if not todo:
        return stats

    # custom_id must be short and [a-zA-Z0-9_-], so use an index
    by_id = {}
    requests = []
    for idx, (doc, h, budget_year, source, pages) in enumerate(todo):
        cid = f"b{idx:04d}"
        by_id[cid] = (doc, h, budget_year, source, pages)
        requests.append({"custom_id": cid,
                         "params": _request_params(pages, budget_year, source)})

    client = _client()
    batch = client.messages.batches.create(requests=requests)
    if progress_fn:
        progress_fn(f"submitted batch {batch.id} with {len(requests)} requests")

    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        if progress_fn:
            c = batch.request_counts
            progress_fn(f"processing... {c.succeeded} succeeded, "
                        f"{c.errored} errored, {c.processing} in flight")
        time.sleep(poll_seconds)

    store = load_store()
    for result in client.messages.batches.results(batch.id):
        doc, h, budget_year, source, pages = by_id[result.custom_id]
        if result.result.type != "succeeded":
            stats["errors"] += 1
            if progress_fn:
                progress_fn(f"{result.result.type}: {doc['filename']}")
            continue
        try:
            parsed = _parse_response(result.result.message)
        except Exception as e:
            stats["errors"] += 1
            if progress_fn:
                progress_fn(f"parse error {doc['filename']}: {e}")
            continue
        store["files"][doc["filename"]] = _record(doc, h, budget_year, source,
                                                  pages, parsed)
        stats["extracted"] += 1

    save_store(store)
    return stats


# ---------------------------------------------------------------------------
# Merging and comparison
# ---------------------------------------------------------------------------

# The latest stage a packet can hold, by meeting month. Claude's stage label
# is unreliable at the edges: a July hearing notice can come back "adopted".
MONTH_STAGE_CAP = {"05": "preliminary", "06": "preliminary", "07": "proposed"}


def _capped_stage(entry: dict, filename: str) -> str:
    if entry.get("source") == "archive":
        # A published budget book is the adopted budget unless it says otherwise.
        return "proposed" if "preliminary" in filename.lower() else "adopted"
    cap = MONTH_STAGE_CAP.get(entry["meeting_date"][5:7])
    if cap and STAGE_RANK[entry["stage"]] > STAGE_RANK[cap]:
        return cap
    return entry["stage"]


def _expected_year(entry: dict) -> int:
    # Entries extracted before archive support only came from packets.
    return entry.get("expected_year") or int(entry["meeting_date"][:4]) + 1


def _entries() -> list[dict]:
    """Extracted documents that hold a budget, highest stage and latest first.

    A budget book is dated January 1 of its year, after the packets that
    adopted it, so among equally adopted sources the book wins.
    """
    store = load_store()
    out = [{**e, "source_file": f, "stage": _capped_stage(e, f)} for f, e in store["files"].items()
           if e.get("stage", "none") != "none"
           and e.get("budget_year") == _expected_year(e)]
    out.sort(key=lambda e: (STAGE_RANK[e["stage"]], e["meeting_date"]), reverse=True)
    return out


def _figure_rank(entry: dict, year: int) -> int:
    """How much to trust a budget-basis figure for `year` from this packet.

    The year's own packets win, adopted first. A later packet's prior-year
    column only fills gaps: its tables sometimes cover a narrower scope (a
    General Fund-only tax line, say) than the year's own citywide figure.
    """
    if entry["budget_year"] == year:
        return STAGE_RANK[entry["stage"]] + 1
    return 1


def _rows_for(entry: dict, key: str, year: int) -> list[dict]:
    return [r for r in entry[key] if r["year"] == year and r["basis"] == "budget"]


def budget_years() -> dict[int, dict]:
    """Merge per-packet extractions into one record per budget year.

    Each headline metric takes the most trusted budget-basis figure for the
    year across all packets (see _figure_rank), so years before the first
    budget packet are filled from later packets' prior-year columns. Line
    items come from the most trusted packet that lists them for the year.
    Drivers and assumptions come from the year's most authoritative packet
    that lists them.
    """
    entries = _entries()
    years: dict[int, dict] = {}

    for e in entries:
        for r in e["headline"]:
            if r["basis"] != "budget":
                continue
            rec = years.setdefault(r["year"], {"year": r["year"], "headline": {}})
            rank = _figure_rank(e, r["year"])
            cur = rec["headline"].get(r["metric"])
            if cur is None or rank > cur["_rank"]:
                rec["headline"][r["metric"]] = {
                    "value": r["value"], "page": r["page"], "source": e["source_file"],
                    "stage": e["stage"] if e["budget_year"] == r["year"]
                    else f"prior-year column, {e['budget_year']} {e['stage']}",
                    "_rank": rank}

    for year, rec in years.items():
        own = [e for e in entries if e["budget_year"] == year]
        rec["stage"] = own[0]["stage"] if own else "prior-year column"
        for m in rec["headline"].values():
            del m["_rank"]
        by_trust = sorted(entries, key=lambda e: _figure_rank(e, year), reverse=True)
        for key in ("funds", "expense_categories", "departments"):
            src = next((e for e in by_trust if _rows_for(e, key, year)), None)
            rec[key] = [{**r, "source": src["source_file"]} for r in _rows_for(src, key, year)] if src else []
        # Later stages restate earlier stages' drivers, so take the list from
        # the most authoritative packet that has one rather than pooling.
        src = next((e for e in own if e["drivers"]), None)
        rec["drivers"] = [{**d, "source": src["source_file"], "stage": src["stage"]}
                          for d in src["drivers"]] if src else []
        src = next((e for e in own if e["assumptions"]), None)
        rec["assumptions"] = [{**a, "source": src["source_file"]}
                              for a in src["assumptions"]] if src else []
        rec["narrative"] = next((e["narrative"] for e in own if e["narrative"]), "")
        rec["sources"] = [{"file": e["source_file"], "stage": e["stage"],
                           "meeting_date": e["meeting_date"]} for e in own]
    return dict(sorted(years.items()))


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _line_diff(entries: list[dict], key: str, name: str,
               year_a: int, year_b: int) -> tuple[list[dict], str | None]:
    """Diff line items between two years.

    Prefers a single packet that lists both years (a budget table with a
    prior-year column): same document, same line definitions, so renamed or
    regrouped lines don't show up as false changes. Falls back to matching
    names across each year's own best packet. Returns (rows, source).
    """
    same_doc = [e for e in entries if _rows_for(e, key, year_a) and _rows_for(e, key, year_b)]
    if same_doc:
        e = max(same_doc, key=lambda e: _figure_rank(e, year_b))
        a_rows, b_rows, source = _rows_for(e, key, year_a), _rows_for(e, key, year_b), e["source_file"]
    else:
        def best(year):
            src = max((e for e in entries if _rows_for(e, key, year)),
                      key=lambda e: _figure_rank(e, year), default=None)
            return _rows_for(src, key, year) if src else []
        a_rows, b_rows, source = best(year_a), best(year_b), None

    a_map = {}
    for r in a_rows:
        a_map[_norm(r[name])] = a_map.get(_norm(r[name]), 0) + r["amount"]
    b_map = {}
    for r in b_rows:
        b_map[_norm(r[name])] = (r[name], b_map.get(_norm(r[name]), ("", 0))[1] + r["amount"])
    out = []
    for k, (label, amount) in b_map.items():
        if k not in a_map:
            continue
        base = a_map[k]
        out.append({"name": label, "from": base, "to": amount, "change": amount - base,
                    "pct": (amount - base) / base * 100 if base else None})
    return out, source


def compare(year_a: int, year_b: int) -> dict:
    """Year-over-year comparison: headline deltas, where the change came from, and why."""
    years = budget_years()
    a, b = years.get(year_a), years.get(year_b)
    if not a or not b:
        missing = [y for y, r in ((year_a, a), (year_b, b)) if not r]
        raise KeyError(f"no extracted budget for {', '.join(map(str, missing))}")

    headline = []
    for key, label in HEADLINE_METRICS.items():
        va = a["headline"].get(key, {}).get("value")
        vb = b["headline"].get(key, {}).get("value")
        if va is None and vb is None:
            continue
        change = vb - va if va is not None and vb is not None else None
        headline.append({"metric": key, "label": label, "from": va, "to": vb, "change": change,
                         "pct": change / va * 100 if change is not None and va else None})

    entries = _entries()
    categories, cat_src = _line_diff(entries, "expense_categories", "label", year_a, year_b)
    total_change = sum(c["change"] for c in categories)
    for c in categories:
        c["share_of_change"] = c["change"] / total_change * 100 if total_change else None
    departments, dept_src = _line_diff(entries, "departments", "department", year_a, year_b)

    return {"from_year": year_a, "to_year": year_b,
            "stages": (a["stage"], b["stage"]),
            "headline": headline,
            "categories": sorted(categories, key=lambda c: c["change"], reverse=True),
            "categories_source": cat_src,
            "departments": sorted(departments, key=lambda d: abs(d["change"]), reverse=True),
            "departments_source": dept_src,
            "drivers": b["drivers"],
            "assumptions": b["assumptions"]}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

SHORT_LABELS = {
    "total_budget_authority": "Total budget",
    "total_expenditures": "Total expenditures",
    "general_fund_expenditures": "General Fund",
    "ad_valorem_tax_levied": "Property tax levied",
    "mill_levy": "Mill levy",
    "revenue_neutral_rate": "Revenue-neutral rate",
    "assessed_valuation": "Assessed valuation",
    "general_fund_ending_balance": "GF ending balance",
    "general_fund_reserve_pct": "GF reserve %",
    "full_time_equivalents": "Staff (FTE)",
}

_MONEY_KEYS = {"total_budget_authority", "total_expenditures", "general_fund_expenditures",
               "ad_valorem_tax_levied", "assessed_valuation", "general_fund_ending_balance"}


def fmt_value(key: str, v) -> str:
    if v is None:
        return "—"
    if key in _MONEY_KEYS:
        return f"${v / 1e6:,.2f}M" if abs(v) >= 1e6 else f"${v:,.0f}"
    if key == "general_fund_reserve_pct":
        return f"{v:.1f}%"
    if key in ("mill_levy", "revenue_neutral_rate"):
        return f"{v:.3f}"
    return f"{v:,.1f}" if isinstance(v, float) and not v.is_integer() else f"{v:,.0f}"


def fmt_money(v: float) -> str:
    sign = "-" if v < 0 else ""
    v = abs(v)
    return f"{sign}${v / 1e6:,.2f}M" if v >= 1e6 else f"{sign}${v:,.0f}"


def render_markdown() -> str:
    """Budget history report: a trend table, then each year's changes and drivers."""
    years = budget_years()
    if not years:
        return "# Prairie Village Budget History\n\nNo budgets extracted yet."

    lines = ["# Prairie Village Budget History", ""]
    lines.append(
        f"Budget years {min(years)}–{max(years)}, extracted from council packets "
        "by an LLM. Figures come from the adopted budget where the packets include "
        "it, otherwise the proposed or preliminary budget; each cell's stage and "
        "source page are listed under that year. Verify against the source packet "
        "before relying on any single figure."
    )

    ys = list(years)
    lines += ["", "## Trend", "",
              "| Metric | " + " | ".join(map(str, ys)) + " |",
              "| --- |" + " ---: |" * len(ys)]
    for key, label in HEADLINE_METRICS.items():
        vals = [years[y]["headline"].get(key, {}).get("value") for y in ys]
        if all(v is None for v in vals):
            continue
        lines.append(f"| {SHORT_LABELS[key]} | " +
                     " | ".join(fmt_value(key, v) for v in vals) + " |")

    for y in reversed(ys):
        rec = years[y]
        lines += ["", f"## {y} budget ({rec['stage']})", ""]
        if rec["narrative"]:
            lines += [rec["narrative"], ""]
        if y - 1 in years:
            cmp = compare(y - 1, y)
            if cmp["categories"]:
                lines += [f"**General Fund change by category, {y - 1} to {y}:**", ""]
                for c in cmp["categories"]:
                    share = f", {c['share_of_change']:.0f}% of the change" if c["share_of_change"] is not None else ""
                    pct = f" ({c['pct']:+.1f}%)" if c["pct"] is not None else ""
                    lines.append(f"- {c['name']}: {fmt_money(c['change'])}{pct}{share}")
                lines.append("")
            if cmp["departments"]:
                lines += ["**Largest department changes:**", ""]
                for d in cmp["departments"][:6]:
                    pct = f" ({d['pct']:+.1f}%)" if d["pct"] is not None else ""
                    lines.append(f"- {d['name']}: {fmt_money(d['change'])}{pct}")
                lines.append("")
        if rec["drivers"]:
            lines += ["**Drivers cited:**", ""]
            seen = set()
            for d in rec["drivers"]:
                if _norm(d["description"]) in seen:
                    continue
                seen.add(_norm(d["description"]))
                amt = f" ({fmt_money(d['amount_change'])})" if d["amount_change"] is not None else ""
                page = f", p.{d['page']}" if d["page"] else ""
                lines.append(f"- [{d['category']}] {d['description']}{amt} — {d['source'][:10]}{page}")
            lines.append("")
        if rec["assumptions"]:
            lines.append("**Assumptions:** " + "; ".join(
                f"{a['item']} {a['value']}" for a in rec["assumptions"]))
            lines.append("")
        lines.append("Sources: " + "; ".join(
            f"{s['file']} ({s['stage']})" for s in rec["sources"]))
    return "\n".join(lines)


def _stage_abbrev(stage: str) -> str:
    return "prior col" if stage.startswith("prior-year") else stage


def render_trend() -> str:
    """Plain-text table of headline metrics by budget year."""
    years = budget_years()
    if not years:
        return "No budgets extracted yet — run `pvc extract-budgets` first."
    ys = list(years)
    rows = [(SHORT_LABELS[key], [fmt_value(key, years[y]["headline"].get(key, {}).get("value")) for y in ys])
            for key in HEADLINE_METRICS
            if any(key in years[y]["headline"] for y in ys)]
    width = max(len(r[0]) for r in rows)
    col = max(9, *(len(v) for _, vals in rows for v in vals))
    out = [f"{'':<{width}}  " + " ".join(f"{y:>{col}}" for y in ys),
           f"{'stage':<{width}}  " + " ".join(f"{_stage_abbrev(years[y]['stage']):>{col}}" for y in ys)]
    out += [f"{name:<{width}}  " + " ".join(f"{v:>{col}}" for v in vals) for name, vals in rows]
    return "\n".join(out)


def render_compare(cmp: dict) -> str:
    """Plain-text year-over-year comparison."""
    a, b = cmp["from_year"], cmp["to_year"]
    out = [f"{a} ({cmp['stages'][0]}) → {b} ({cmp['stages'][1]})", ""]
    for h in cmp["headline"]:
        pct = f" ({h['pct']:+.1f}%)" if h["pct"] is not None else ""
        out.append(f"  {SHORT_LABELS[h['metric']]:<22} {fmt_value(h['metric'], h['from']):>12} → "
                   f"{fmt_value(h['metric'], h['to']):>12}{pct}")
    if cmp["categories"]:
        out += ["", "Where the General Fund change came from:"]
        for c in cmp["categories"]:
            share = f"  {c['share_of_change']:5.0f}% of change" if c["share_of_change"] is not None else ""
            out.append(f"  {c['name']:<40} {fmt_money(c['change']):>12}{share}")
    if cmp["departments"]:
        out += ["", "Largest department changes:"]
        for d in cmp["departments"][:10]:
            pct = f" ({d['pct']:+.1f}%)" if d["pct"] is not None else ""
            out.append(f"  {d['name']:<40} {fmt_money(d['change']):>12}{pct}")
    if cmp["drivers"]:
        out += ["", f"Drivers cited for {b}:"]
        seen = set()
        for d in cmp["drivers"]:
            if _norm(d["description"]) in seen:
                continue
            seen.add(_norm(d["description"]))
            amt = f" [{fmt_money(d['amount_change'])}]" if d["amount_change"] is not None else ""
            page = f" p.{d['page']}" if d["page"] else ""
            out.append(f"  - ({d['category']}) {d['description']}{amt}  — {d['source'][:10]}{page}")
    if cmp["assumptions"]:
        out += ["", "Assumptions: " + "; ".join(f"{x['item']} {x['value']}" for x in cmp["assumptions"])]
    return "\n".join(out)
