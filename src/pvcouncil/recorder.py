"""Johnson County Recorder of Deeds (TrakRecord Public Access, subscription site).

The county's records search is a DTS TrakRecord app. A subscriber logs in
through an ASP.NET WebForms page; the search page then carries session
values that every search posts back:

  GET  /                      -> login form (__VIEWSTATE, __EVENTVALIDATION)
  POST /                      -> txtUsername, txtPassword, btnLogin
  GET  /views/search          -> page holding SessionGuid, SessionId, SessionTicket
  POST /api/v2Search          -> form-encoded search, JSON results (DataTables)

Legal-description filters go in LegalParameters as "<field>:<LIKE|IS>:<value>",
joined with "|" when there are several. Field 1 is Subdivision, 2 Block,
3 Lot, 8 PIN (the county parcel ID, e.g. "OP55000002 0030").

Document categories and types are numeric IDs:
  category 6 = PLAT (types 7, 105, 112, 115)
  category 7 = OTHER, which holds the restriction instruments:
     63 Declaration of Restrictions
     74 Declaration of Home Owners Association
  11007 Certificate of Release of Prohibitive Covenants

Credentials come from JOCO_RECORDER_USER / JOCO_RECORDER_PASS in .env
(JOCO_RECORD_PASS is accepted too). They are never logged or stored.
"""

import html
import json
import os
import re
import time
from http.cookiejar import CookieJar
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, Request, build_opener

from pvcouncil.download import USER_AGENT

BASE = "https://rtadocument.jocogov.org/TrakRecordPublicAccessSubscription"
SEARCH_URL = f"{BASE}/api/v2Search"

LEGAL_FIELDS = {"subdivision": 1, "block": 2, "lot": 3, "pin": 8}
PLAT_CATEGORY = 6
PLAT_DOC_TYPES = [7, 105, 112, 115]
OTHER_CATEGORY = 7
RESTRICTION_DOC_TYPES = {
    63: "Declaration of Restrictions",
    74: "Declaration of Home Owners Association",
    11007: "Certificate of Release of Prohibitive Covenants",
}

SESSION_KEYS = ("SessionGuid", "SessionId", "SessionTicket")
COLUMNS = ("ReferenceDisplayValue,RecordedDate,dt.Value,LastName,FirstName,nt.Value Role,"
           "RelatedName,PropertyLegalShort,FirstMarginalDisplayValue,InstrumentNumber,"
           "Consideration,InstrumentStatus,sri.Remarks")
PAGE_SIZE = 100
SEARCH_TIMEOUT = 180  # broad searches can take minutes server-side
REQUEST_DELAY = 1.0  # seconds between searches; be polite to the county server


class RecorderError(Exception):
    pass


def credentials() -> tuple[str, str]:
    user = os.environ.get("JOCO_RECORDER_USER", "")
    pw = os.environ.get("JOCO_RECORDER_PASS") or os.environ.get("JOCO_RECORD_PASS", "")
    if not user or not pw:
        raise RecorderError("Set JOCO_RECORDER_USER and JOCO_RECORDER_PASS in .env")
    return user, pw


def _hidden_fields(page: str) -> dict:
    fields = {}
    for m in re.finditer(r'<input[^>]*type="hidden"[^>]*>', page):
        tag = m.group(0)
        name = re.search(r'name="([^"]+)"', tag)
        value = re.search(r'value="([^"]*)"', tag)
        if name:
            fields[name.group(1)] = html.unescape(value.group(1)) if value else ""
    return fields


_SESSION_PATTERNS = [
    # hidden input or element attribute: id="SessionGuid" ... value="..."
    r'(?:id|name)=["\']{key}["\'][^>]*?value=["\']([^"\']+)["\']',
    r'value=["\']([^"\']+)["\'][^>]*?(?:id|name)=["\']{key}["\']',
    # script: SessionGuid: "...", SessionGuid = '...', "SessionGuid":"..."
    r'["\']?{key}["\']?\s*[:=]\s*["\']([^"\']+)["\']',
]


def _session_values(page: str) -> dict:
    """Pull the session values from the search page's `var cs={...}` settings.

    The page's own search code sends cs.SessionId as both SessionId and SessionGuid.
    """
    found = {}
    for key in ("SessionId", "SessionTicket"):
        for pat in _SESSION_PATTERNS:
            m = re.search(pat.format(key=key), page)
            if m and m.group(1).strip():
                found[key] = m.group(1).strip()
                break
    if "SessionId" in found:
        found["SessionGuid"] = found["SessionId"]
    return found


class Recorder:
    """A logged-in session with the recorder's search API."""

    def __init__(self, delay: float = REQUEST_DELAY):
        self.delay = delay
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))
        self.session: dict = {}
        self._last = 0.0

    def _open(self, url: str, data: dict | None = None, headers: dict | None = None,
              timeout: int = 60) -> str:
        body = urlencode(data).encode() if data is not None else None
        req = Request(url, data=body, headers={"User-Agent": USER_AGENT, **(headers or {})})
        with self.opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def login(self) -> "Recorder":
        user, pw = credentials()
        form = _hidden_fields(self._open(f"{BASE}/"))
        form.update({"txtUsername": user, "txtPassword": pw, "btnLogin": "Login"})
        self._open(f"{BASE}/", form)
        page = self._open(f"{BASE}/views/search")
        self.session = _session_values(page)
        missing = [k for k in SESSION_KEYS if k not in self.session]
        if missing:
            raise RecorderError(
                f"Logged-in search page is missing {', '.join(missing)}; "
                "the login may have failed or the site changed. Run `pvc recorder-check`.")
        return self

    def search(self, legal: list[tuple[str, str, str]] = (), categories: list[int] = (),
               doc_types: list[int] = (), order: str = "RecordedDate:asc",
               max_rows: int | None = None, name: str = "", name_match: str = "Like") -> list[dict]:
        """Run a search and return every result row as a dict keyed by column name.

        legal is a list of (field, "LIKE"|"IS", value) with field in LEGAL_FIELDS.
        """
        legal_param = "|".join(f"{LEGAL_FIELDS[f]}:{op}:{v}" for f, op, v in legal)
        rows, first, total, draw = [], 1, None, 1
        while total is None or first <= total:
            last = first + PAGE_SIZE - 1
            data = self._search_page(legal_param, categories, doc_types, order, first, last, draw,
                                     name, name_match)
            total = data.get("recordsTotal", 0)
            cols = data.get("Columns", [])
            for r in data.get("aaData", []):
                rows.append(_row_dict(cols, r))
            if max_rows and len(rows) >= max_rows:
                return rows[:max_rows]
            first, draw = last + 1, draw + 1
        return rows

    def _search_page(self, legal_param, categories, doc_types, order, first, last, draw,
                     name="", name_match="Like") -> dict:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        form = {
            "Book": "", "BookType": "", "Bookpage": "",
            "CategoryDocumentTypes": ",".join(map(str, doc_types)),
            "CategoryTypes": ",".join(map(str, categories)),
            "Columns": f" {COLUMNS} ",
            "Consideration1": "", "Consideration2": "", "DocumentId": "",
            "Draw": str(draw), "FilterWhere": "", "FirstName1": "", "FirstName2": "",
            "FirstNameAndOr": "Or", "FirstNameComparison": "Like",
            "FirstRecordNum": str(first), "GridData": "", "IncludeCrossReferences": "",
            "InstrumentId": "", "InstrumentReferenceIds": "", "IsCountLookupEnabled": "true",
            "LastNameComparison": name_match, "LastRecordNum": str(last),
            "LegalParameters": legal_param, "Message": "", "NameOrganization": name,
            "OrderBy": order, "ParentId": "", "Party": "any",
            "RecordedDate1": "", "RecordedDate2": "", "Remarks": "",
            "SavedSearchId": "", "SearchHistoryId": "",
            "SearchType": "Advanced", "SearchTypeClient": "", "SearchTypeResults": "Summary",
            "TotalRecords": "0", "isDetailPrint": "false",
            **self.session,
        }
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        for attempt in range(3):
            try:
                text = self._open(SEARCH_URL, form, headers, timeout=SEARCH_TIMEOUT)
                break
            except (TimeoutError, OSError):
                if attempt == 2:
                    raise RecorderError("Search timed out three times")
                time.sleep(5 * (attempt + 1))
        self._last = time.monotonic()
        try:
            data = json.loads(text)
            if isinstance(data, str):  # the API double-encodes its JSON
                data = json.loads(data)
        except json.JSONDecodeError:
            raise RecorderError("Search returned non-JSON (session expired?)")
        if data.get("Messages"):
            raise RecorderError(f"Search error: {data['Messages']}")
        return data


def _row_dict(columns: list[str], row: list) -> dict:
    """Map a result row onto column names.

    Rows carry one leading value before the declared columns, and column
    names can be SQL fragments ("dt.Value", "nt.Value Role", "case ... end X"),
    so key each on its last word.
    """
    out = {}
    for i, col in enumerate(columns):
        key = col.strip().split()[-1].split(".")[-1]
        out[key] = row[i + 1] if i + 1 < len(row) else None
    return out


def check() -> dict:
    """Log in and report which session values were found, never the values themselves."""
    rec = Recorder()
    user, pw = credentials()
    login_page = rec._open(f"{BASE}/")
    form = _hidden_fields(login_page)
    report = {"login_form_fields": sorted(form)}
    form.update({"txtUsername": user, "txtPassword": pw, "btnLogin": "Login"})
    after = rec._open(f"{BASE}/", form)
    report["still_on_login_form"] = 'name="txtPassword"' in after
    page = rec._open(f"{BASE}/views/search")
    report["search_page_has_login_form"] = 'name="txtPassword"' in page
    found = _session_values(page)
    report["session_values"] = {k: (f"found ({len(found[k])} chars)" if k in found else "missing")
                                for k in SESSION_KEYS}
    report["mentions"] = {k: page.count(k) for k in SESSION_KEYS}
    if len(found) == len(SESSION_KEYS):
        rec.session = found
        rows = rec.search([("pin", "LIKE", "OP55000002 0030")], max_rows=5)
        report["test_search_rows"] = len(rows)
        report["test_search_columns"] = sorted(rows[0]) if rows else []
    return report
