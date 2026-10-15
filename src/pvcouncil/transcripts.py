"""Hand-checked transcriptions of regions OCR couldn't read on its own.

The deed viewer lets you box a region of a page, OCR it at a higher zoom with
clean-up (invert, contrast, threshold), correct the text, and save it. Saved
regions live in deeds/ocr/manual/transcripts.json (gitignored: the text can
name signers and owners), keyed by document: 'p:<id>' for a plat, 'r:<id>' for
a restriction instrument. `pvc plat-ocr` and `pvc restriction-read` add them to
each document's OCR text before looking for restriction terms.

A box is stored in fractions of the unrotated page, [x0, y0, x1, y1], so it
lands in the same place at any zoom or rotation. Each region also records its
source: the document's book and page, recording date, page count, and a
fingerprint (SHA-256 prefix) of the image file it was drawn on. If that image is
later replaced (re-downloaded, rescanned), the region is flagged stale: its box
may no longer sit on the text it transcribes.
"""

import hashlib
import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

from pvcouncil import config, plats  # noqa: F401 - config loads .env

# PVC_TRANSCRIPTS_DIR (in .env) keeps them outside the checkout, in a private git
# repository: they're made by hand, so each save and delete is committed there
_dir = os.environ.get("PVC_TRANSCRIPTS_DIR")
PATH = (Path(_dir).expanduser() if _dir else plats.DEEDS_DIR / "ocr" / "manual") / "transcripts.json"
_lock = threading.Lock()


def load() -> dict[str, list[dict]]:
    return json.loads(PATH.read_text()) if PATH.exists() else {}


def _write(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    tmp.replace(PATH)


def _commit(message: str) -> None:
    """Commit the transcriptions if they live in a git repository. Messages carry only
    record keys and region ids, never text."""
    repo = PATH.parent
    if not (repo / ".git").exists():
        return
    subprocess.run(["git", "-C", str(repo), "add", PATH.name], capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", message], capture_output=True)


def for_key(key: str) -> list[dict]:
    return load().get(key, [])


_fingerprints: dict[tuple, str] = {}


def fingerprint(path: str | Path) -> str:
    """SHA-256 prefix of an image file, cached by path, size and modification time."""
    st = Path(path).stat()
    k = (str(path), st.st_size, st.st_mtime_ns)
    if k not in _fingerprints:
        _fingerprints[k] = hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    return _fingerprints[k]


def is_stale(region: dict, current: str) -> bool | None:
    """True if the region was drawn on a different image than the current one; None if unknown."""
    fp = (region.get("source") or {}).get("fingerprint")
    return None if fp is None else fp != current


def save(key: str, region: dict, source: dict) -> dict:
    """Add a region, or replace the one with the same id. source carries the document's
    book_page, recorded date, page count, and the fingerprint of the image the box was drawn on."""
    keep = ("page", "box", "rot", "dpi", "invert", "threshold", "psm", "engine", "text")
    r = {k: region[k] for k in keep if k in region}
    r["source"] = {k: source[k] for k in ("book_page", "recorded", "pages", "fingerprint")}
    r["id"] = region.get("id") or uuid.uuid4().hex[:10]
    r["saved"] = time.strftime("%Y-%m-%d %H:%M")
    with _lock:
        data = load()
        items = [x for x in data.get(key, []) if x["id"] != r["id"]]
        items.append(r)
        data[key] = sorted(items, key=lambda x: (x["page"], x["box"][1], x["box"][0]))
        _write(data)
        _commit(f"Save {key} page {r['page']} region {r['id']}")
    return r


def delete(key: str, region_id: str) -> None:
    with _lock:
        data = load()
        data[key] = [x for x in data.get(key, []) if x["id"] != region_id]
        if not data[key]:
            del data[key]
        _write(data)
        _commit(f"Delete {key} region {region_id}")


def text_for(key: str, data: dict | None = None) -> str:
    """The saved transcriptions of a document, in page order, ready to add to its OCR text."""
    items = (data if data is not None else load()).get(key, [])
    return "\n\n".join(f"[transcribed, page {x['page']}]\n{x['text']}" for x in items if x.get("text", "").strip())


def attribute(items: list[dict], found_by_ocr: set[str], terms_of) -> dict[str, list[dict]]:
    """Which terms only transcriptions found, and where: {term: [{page, region}, ...]}.

    terms_of(text) returns the terms in a piece of text; a term OCR already found on its
    own is not credited to a transcription."""
    out: dict[str, list[dict]] = {}
    for x in items:
        for t in terms_of(x.get("text", "")):
            if t not in found_by_ocr:
                out.setdefault(t, []).append({"page": x["page"], "region": x["id"]})
    return out
