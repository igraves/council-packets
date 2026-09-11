"""Download council documents from the CivicClerk portal.

Prairie Village migrated from pvkansas.com to CivicClerk (~March 2026).
CivicClerk exposes an OData API; no authentication required:

  GET /v1/Events?$filter=...          -> meetings with embedded publishedFiles
  GET /v1/Meetings/GetMeetingFileStream(fileId=N,plainText=false) -> PDF bytes
"""

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

from pvcouncil.config import DOCS_DIR, METADATA_PATH

API_BASE = "https://prairievillageks.api.civicclerk.com/v1"

# CivicClerk publishedFiles.type -> corpus subdirectory
# Bare agendas are skipped: the agenda packet supersedes them.
TYPE_DIRS = {
    "Agenda Packet": "packets",
    "Minutes": "minutes",
}
SKIP_TYPES = {"Agenda"}


def _get_json(url: str) -> dict:
    req = Request(url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=60) as resp:
        return json.load(resp)


def fetch_events(after: date, category: str = "City Council") -> list[dict]:
    """Fetch meetings after a date, following OData pagination."""
    filt = f"startDateTime gt {after.isoformat()}T00:00:00Z and categoryName eq '{category}'"
    url = f"{API_BASE}/Events?$filter={quote(filt)}&$orderby=startDateTime"
    events = []
    while url:
        data = _get_json(url)
        events.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return events


def _safe_name(name: str) -> str:
    return re.sub(r'[/\\:]', "-", name).strip()


def download_file(file_id: int, dest: Path) -> int:
    """Stream one document to dest. Returns bytes written."""
    url = f"{API_BASE}/Meetings/GetMeetingFileStream(fileId={file_id},plainText=false)"
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = Request(url, headers={"Accept": "application/pdf"})
    written = 0
    with urlopen(req, timeout=300) as resp, open(tmp, "wb") as f:
        while chunk := resp.read(1 << 16):
            f.write(chunk)
            written += len(chunk)
    tmp.rename(dest)
    return written


def latest_local_date() -> date:
    """Newest meeting date present on disk (from YYYY-MM-DD filename prefixes)."""
    latest = date(2026, 1, 1)
    for sub in ("packets", "minutes", "other"):
        d = DOCS_DIR / sub
        if not d.exists():
            continue
        for f in d.iterdir():
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})_", f.name)
            if m:
                latest = max(latest, date(*map(int, m.groups())))
    return latest


def _append_metadata(new_entries: list[dict]) -> None:
    meta = json.loads(METADATA_PATH.read_text()) if METADATA_PATH.exists() else []
    existing = {(e.get("date_iso"), e.get("event")) for e in meta}
    added = [e for e in new_entries if (e["date_iso"], e["event"]) not in existing]
    if added:
        meta.extend(added)
        METADATA_PATH.write_text(json.dumps(meta, indent=2))


def download_new(after: date | None = None, dry_run: bool = False, progress_fn=None) -> dict:
    """Download all packet/minutes PDFs newer than `after` (default: newest on disk).

    Returns stats: {events, downloaded, skipped, no_files}
    """
    if after is None:
        after = latest_local_date()

    events = fetch_events(after)
    stats = {"events": len(events), "downloaded": 0, "skipped": 0, "no_files": 0}
    meta_entries = []

    for event in events:
        event_date = event["eventDate"][:10]
        files = event.get("publishedFiles") or []
        if not files:
            stats["no_files"] += 1
            continue

        docs_meta = []
        for f in files:
            ftype = f.get("type", "")
            if ftype in SKIP_TYPES:
                continue
            subdir = TYPE_DIRS.get(ftype, "other")
            name = _safe_name(f.get("name") or f"{event_date} {ftype}")
            dest = DOCS_DIR / subdir / f"{event_date}_{name}.pdf"

            if dest.exists():
                stats["skipped"] += 1
                if progress_fn:
                    progress_fn(event_date, dest.name, "exists")
                continue

            if dry_run:
                stats["downloaded"] += 1
                if progress_fn:
                    progress_fn(event_date, dest.name, "would download")
                continue

            dest.parent.mkdir(parents=True, exist_ok=True)
            size = download_file(f["fileId"], dest)
            stats["downloaded"] += 1
            docs_meta.append({
                "type": "minutes" if subdir == "minutes" else "agenda",
                "name": name,
                "url": f"{API_BASE}/Meetings/GetMeetingFileStream(fileId={f['fileId']},plainText=false)",
            })
            if progress_fn:
                progress_fn(event_date, dest.name, f"{size // 1024} KB")

        if docs_meta:
            y, m, d = event_date.split("-")
            meta_entries.append({
                "event": event.get("eventName", "City Council Meeting"),
                "date_raw": f"{m}/{d}/{y}",
                "date_iso": event_date,
                "docs": docs_meta,
            })

    if not dry_run:
        _append_metadata(meta_entries)

    return stats
