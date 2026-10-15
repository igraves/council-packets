"""Hand-checked transcriptions of regions OCR couldn't read on its own.

The deed viewer lets you box a region of a page, OCR it at a higher zoom with
clean-up (invert, contrast, threshold), correct the text, and save it. Saved
regions live in deeds/ocr/manual/transcripts.json (gitignored: the text can
name signers and owners), keyed by document: 'p:<id>' for a plat, 'r:<id>' for
a restriction instrument. `pvc plat-ocr` and `pvc restriction-read` add them to
each document's OCR text before looking for restriction terms.

A box is stored in fractions of the unrotated page, [x0, y0, x1, y1], so it
lands in the same place at any zoom or rotation.
"""

import json
import threading
import time
import uuid

from pvcouncil import plats

PATH = plats.DEEDS_DIR / "ocr" / "manual" / "transcripts.json"
_lock = threading.Lock()


def load() -> dict[str, list[dict]]:
    return json.loads(PATH.read_text()) if PATH.exists() else {}


def _write(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n")
    tmp.replace(PATH)


def for_key(key: str) -> list[dict]:
    return load().get(key, [])


def save(key: str, region: dict) -> dict:
    """Add a region, or replace the one with the same id."""
    keep = ("page", "box", "rot", "dpi", "invert", "threshold", "psm", "text")
    r = {k: region[k] for k in keep if k in region}
    r["id"] = region.get("id") or uuid.uuid4().hex[:10]
    r["saved"] = time.strftime("%Y-%m-%d %H:%M")
    with _lock:
        data = load()
        items = [x for x in data.get(key, []) if x["id"] != r["id"]]
        items.append(r)
        data[key] = sorted(items, key=lambda x: (x["page"], x["box"][1], x["box"][0]))
        _write(data)
    return r


def delete(key: str, region_id: str) -> None:
    with _lock:
        data = load()
        data[key] = [x for x in data.get(key, []) if x["id"] != region_id]
        if not data[key]:
            del data[key]
        _write(data)


def text_for(key: str, data: dict | None = None) -> str:
    """The saved transcriptions of a document, in page order, ready to add to its OCR text."""
    items = (data if data is not None else load()).get(key, [])
    return "\n\n".join(f"[transcribed, page {x['page']}]\n{x['text']}" for x in items if x.get("text", "").strip())
