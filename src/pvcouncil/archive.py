"""Download reports from the city website's Archive Center.

prairievillageks.gov runs CivicPlus, whose Archive Center groups published
reports (budgets, ACFRs, quarterly financials, police reports) by category:

  GET /Archive.aspx                     -> category dropdowns (amidDDN<AMID>)
  GET /Archive.aspx?AMID=N              -> links to Archive.aspx?ADID=M
  GET /ArchiveCenter/ViewFile/Item/M    -> PDF bytes

These documents have no meeting date, so the YYYY-MM-DD prefix the indexer
relies on is derived from the title (see _doc_date).
"""

import html
import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen

from pvcouncil.config import DOCS_DIR
from pvcouncil.download import USER_AGENT, _safe_name

SITE = "https://www.prairievillageks.gov"
ARCHIVE_URL = f"{SITE}/Archive.aspx"
VIEW_URL = SITE + "/ArchiveCenter/ViewFile/Item/{adid}"

ARCHIVE_DIR = DOCS_DIR / "archive"
MANIFEST_PATH = ARCHIVE_DIR / "archive.json"

QUARTER_END = {"1st": "03-31", "2nd": "06-30", "3rd": "09-30", "4th": "12-31"}
MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}


def _get_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=60) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(fragment))).strip()


def fetch_categories() -> dict[int, str]:
    """Archive categories on the master page: {AMID: name}."""
    page = _get_html(ARCHIVE_URL)
    select = re.search(r'<select[^>]*ArchiveMasterID.*?</select>', page, re.S | re.I)
    if not select:
        return {}
    return {int(v): _text(t) for v, t in
            re.findall(r'<option value="?(\d+)"?[^>]*>([^<]*)', select.group(0))
            if int(v) > 0}


def fetch_items(amid: int) -> list[dict]:
    """Documents in one category: [{adid, title}], as listed (newest first)."""
    page = _get_html(f"{ARCHIVE_URL}?AMID={amid}")
    items, seen = [], set()
    for adid, label in re.findall(r'href="Archive\.aspx\?ADID=(\d+)"[^>]*>(.*?)</a>', page, re.S):
        if adid in seen:
            continue
        seen.add(adid)
        title = re.sub(r"\s*\(PDF\)\s*$", "", _text(label), flags=re.I)
        items.append({"adid": int(adid), "title": title})
    return items


def _doc_date(title: str, category: str) -> str:
    """Sortable date for a report, from its title.

    An explicit date in the title wins ("...Book September 8, 2026"). A
    quarterly report takes its quarter's end, and a budget takes January 1 of
    its budget year, since it is adopted the year before. Other annual reports
    take December 31 of the year they cover.
    """
    m = re.search(r"\b([A-Z][a-z]+) (\d{1,2}), (\d{4})\b", title)
    if m and m.group(1).lower() in MONTHS:
        return f"{m.group(3)}-{MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    year = re.search(r"\b(19|20)\d{2}\b", title)
    if not year:
        return "unknown"
    y = year.group(0)
    q = re.search(r"\b(1st|2nd|3rd|4th) Quarter\b", title, re.I)
    if q:
        return f"{y}-{QUARTER_END[q.group(1).lower()]}"
    if "budget" in category.lower():
        return f"{y}-01-01"
    return f"{y}-12-31"


def load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text())
    return {"items": {}}


def _download_pdf(adid: int, dest: Path) -> int:
    req = Request(VIEW_URL.format(adid=adid),
                  headers={"Accept": "application/pdf", "User-Agent": USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".part")
    written = 0
    with urlopen(req, timeout=300) as resp:
        if "pdf" not in resp.headers.get("Content-Type", "").lower():
            raise ValueError(f"not a PDF ({resp.headers.get('Content-Type')})")
        with open(tmp, "wb") as f:
            while chunk := resp.read(1 << 16):
                f.write(chunk)
                written += len(chunk)
    tmp.rename(dest)
    return written


def download_archive(amids: list[int] | None = None, dry_run: bool = False,
                     delay: float = 0.5, progress_fn=None) -> dict:
    """Download Archive Center documents not already on disk.

    amids limits the crawl to those categories. Returns stats:
    {categories, listed, downloaded, skipped, errors}.
    """
    categories = fetch_categories()
    if amids:
        categories = {a: n for a, n in categories.items() if a in amids}
    manifest = load_manifest()
    stats = {"categories": len(categories), "listed": 0, "downloaded": 0,
             "skipped": 0, "errors": 0}

    for amid, category in categories.items():
        for item in fetch_items(amid):
            stats["listed"] += 1
            key = str(item["adid"])
            known = manifest["items"].get(key)
            if known and (ARCHIVE_DIR / known["file"]).exists():
                stats["skipped"] += 1
                if progress_fn:
                    progress_fn(category, known["file"], "exists")
                continue

            name = f"{_doc_date(item['title'], category)}_{_safe_name(item['title'])}.pdf"
            if dry_run:
                stats["downloaded"] += 1
                if progress_fn:
                    progress_fn(category, name, "would download")
                continue

            ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                size = _download_pdf(item["adid"], ARCHIVE_DIR / name)
            except Exception as e:
                stats["errors"] += 1
                if progress_fn:
                    progress_fn(category, name, f"ERROR: {e}")
                continue
            manifest["items"][key] = {
                "amid": amid, "category": category, "title": item["title"],
                "file": name, "url": VIEW_URL.format(adid=item["adid"]), "bytes": size,
            }
            # Save as we go so an interrupted crawl doesn't re-fetch finished files.
            MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
            stats["downloaded"] += 1
            if progress_fn:
                progress_fn(category, name, f"{size // 1024} KB")
            time.sleep(delay)

    return stats
