"""Extract voting records from council minutes via Claude structured outputs.

Each minutes PDF is sent to Claude with a JSON schema; the result is stored
in votes.json keyed by source file, with size+mtime hashing for incremental
re-extraction (same pattern as the search index tracker).
"""

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

from pvcouncil.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, PROJECT_ROOT
from pvcouncil.extract import extract_text
from pvcouncil.index import discover_docs, _file_hash

VOTES_PATH = PROJECT_ROOT / "votes.json"

VOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "present": {
            "type": "array", "items": {"type": "string"},
            "description": "Council members recorded as present, surnames as printed, no titles",
        },
        "absent": {
            "type": "array", "items": {"type": "string"},
            "description": "Council members recorded as absent",
        },
        "votes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item": {
                        "type": "string",
                        "description": "What was voted on, in one sentence, including ordinance/resolution numbers when given",
                    },
                    "motion_type": {
                        "type": "string",
                        "enum": ["ordinance", "resolution", "consent_agenda",
                                 "contract_or_purchase", "appointment",
                                 "procedural", "other"],
                    },
                    "mover": {"type": "string", "description": "Who made the motion; empty string if not recorded"},
                    "seconder": {"type": "string", "description": "Who seconded; empty string if not recorded"},
                    "result": {
                        "type": "string",
                        "enum": ["passed", "failed", "tabled", "withdrawn", "unknown"],
                    },
                    "yes": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Members explicitly recorded voting yes (e.g. in a roll call). Empty if not individually named.",
                    },
                    "no": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Members explicitly recorded voting no. Empty if none named.",
                    },
                    "abstain": {"type": "array", "items": {"type": "string"}},
                    "tally": {
                        "type": "string",
                        "description": "Numeric tally as recorded, e.g. '10-2', '12-0', or 'unanimous' / 'voice vote' / '' if not recorded",
                    },
                },
                "required": ["item", "motion_type", "mover", "seconder",
                             "result", "yes", "no", "abstain", "tally"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["present", "absent", "votes"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You extract voting records from city council meeting minutes for "
    "Prairie Village, Kansas. Extract every formal motion that was voted on, "
    "including consent agenda approvals, ordinances, resolutions, contracts, "
    "appointments, and procedural motions (approving minutes, adjourning into "
    "executive session, tabling). Rules:\n"
    "- Use member surnames exactly as printed; drop titles like "
    "Councilmember, Council President, Dr., or Mayor.\n"
    "- Only list names in yes/no/abstain when the minutes explicitly record "
    "how that person voted (roll call, or 'X and Y voting nay'). Never infer "
    "an individual's vote from attendance. It is normal for yes to be empty "
    "on a voice vote.\n"
    "- Record the numeric tally exactly as printed when present.\n"
    "- The mayor normally votes only to break ties; include the mayor in a "
    "vote list only when the minutes say the mayor voted."
)


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def _request_params(minutes_text: str) -> dict:
    """Messages API params for one extraction — shared by direct and batch paths."""
    return {
        "model": CLAUDE_MODEL,
        # Covers adaptive thinking + JSON output; vote-heavy meetings can be long
        "max_tokens": 16000,
        "system": SYSTEM_PROMPT,
        "output_config": {"format": {"type": "json_schema", "schema": VOTE_SCHEMA}},
        "messages": [{
            "role": "user",
            "content": (
                "Extract all voting records from these council meeting "
                f"minutes:\n\n{minutes_text}"
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


def extract_votes_text(client: anthropic.Anthropic, minutes_text: str) -> dict:
    """Run one minutes document through Claude; returns the schema dict."""
    return _parse_response(client.messages.create(**_request_params(minutes_text)))


def load_store() -> dict:
    if VOTES_PATH.exists():
        return json.loads(VOTES_PATH.read_text())
    return {"files": {}}


def save_store(store: dict) -> None:
    VOTES_PATH.write_text(json.dumps(store, indent=1))


def extract_all(force: bool = False, limit: int | None = None,
                workers: int = 4, progress_fn=None) -> dict:
    """Extract votes from all minutes docs not yet processed (by file hash).

    Returns stats: {total, extracted, skipped, errors, votes_found}
    """
    store = load_store()
    docs = [d for d in discover_docs() if d["doc_type"] == "minutes"]

    todo = []
    skipped = 0
    for doc in docs:
        h = _file_hash(doc["path"])
        entry = store["files"].get(doc["filename"])
        if entry and entry.get("hash") == h and not force:
            skipped += 1
            continue
        todo.append((doc, h))
    if limit:
        todo = todo[:limit]

    stats = {"total": len(docs), "extracted": 0, "skipped": skipped,
             "errors": 0, "votes_found": 0}
    if not todo:
        return stats

    client = _client()

    def work(doc, h):
        text = extract_text(doc["path"])
        if not text.strip():
            raise RuntimeError("no extractable text")
        result = extract_votes_text(client, text)
        return doc, h, result

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, doc, h): doc for doc, h in todo}
        for fut in as_completed(futures):
            doc = futures[fut]
            done += 1
            try:
                doc, h, result = fut.result()
                store["files"][doc["filename"]] = {
                    "hash": h,
                    "meeting_date": doc["meeting_date"],
                    "present": result["present"],
                    "absent": result["absent"],
                    "votes": result["votes"],
                }
                stats["extracted"] += 1
                stats["votes_found"] += len(result["votes"])
                save_store(store)  # checkpoint so an interrupt loses little
                if progress_fn:
                    progress_fn(done, len(todo), doc["filename"],
                                f"{len(result['votes'])} votes")
            except Exception as e:
                stats["errors"] += 1
                if progress_fn:
                    progress_fn(done, len(todo), doc["filename"], f"ERROR: {e}")

    return stats


def extract_all_batch(force: bool = False, limit: int | None = None,
                      poll_seconds: int = 30, progress_fn=None) -> dict:
    """Extract votes via the Message Batches API (50% token cost, async).

    Submits all pending minutes as one batch, polls until it ends, then
    merges results into votes.json. Same incremental tracking as extract_all.
    Returns stats: {total, extracted, skipped, errors, votes_found}
    """
    import time

    store = load_store()
    docs = [d for d in discover_docs() if d["doc_type"] == "minutes"]

    todo = []
    skipped = 0
    for doc in docs:
        h = _file_hash(doc["path"])
        entry = store["files"].get(doc["filename"])
        if entry and entry.get("hash") == h and not force:
            skipped += 1
            continue
        todo.append((doc, h))
    if limit:
        todo = todo[:limit]

    stats = {"total": len(docs), "extracted": 0, "skipped": skipped,
             "errors": 0, "votes_found": 0}
    if not todo:
        return stats

    # Build requests; custom_id must be short and [a-zA-Z0-9_-], so use an
    # index and keep a mapping back to the document.
    by_id = {}
    requests = []
    for idx, (doc, h) in enumerate(todo):
        text = extract_text(doc["path"])
        if not text.strip():
            stats["errors"] += 1
            if progress_fn:
                progress_fn(f"skip {doc['filename']}: no extractable text")
            continue
        cid = f"d{idx:04d}"
        by_id[cid] = (doc, h)
        requests.append({"custom_id": cid, "params": _request_params(text)})

    if not requests:
        return stats

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

    for result in client.messages.batches.results(batch.id):
        doc, h = by_id[result.custom_id]
        if result.result.type == "succeeded":
            try:
                parsed = _parse_response(result.result.message)
            except Exception as e:
                stats["errors"] += 1
                if progress_fn:
                    progress_fn(f"parse error {doc['filename']}: {e}")
                continue
            store["files"][doc["filename"]] = {
                "hash": h,
                "meeting_date": doc["meeting_date"],
                "present": parsed["present"],
                "absent": parsed["absent"],
                "votes": parsed["votes"],
            }
            stats["extracted"] += 1
            stats["votes_found"] += len(parsed["votes"])
        else:
            stats["errors"] += 1
            if progress_fn:
                progress_fn(f"{result.result.type}: {doc['filename']}")

    save_store(store)
    return stats


def query_votes(member: str | None = None, side: str | None = None,
                result: str | None = None, search: str | None = None,
                date_from: str | None = None, date_to: str | None = None) -> list[dict]:
    """Flatten the store into vote records matching the filters.

    side: 'yes'/'no'/'abstain' — restricts the member match to that list.
    """
    store = load_store()
    out = []
    for filename, entry in store["files"].items():
        date = entry.get("meeting_date", "unknown")
        if date_from and date < date_from:
            continue
        if date_to and date > date_to:
            continue
        for v in entry["votes"]:
            if result and v["result"] != result:
                continue
            if search and search.lower() not in v["item"].lower():
                continue
            if member:
                m = member.lower()
                sides = [side] if side else ["yes", "no", "abstain"]
                names = [n for s in sides for n in v.get(s, [])]
                if side is None:
                    names += [v.get("mover", ""), v.get("seconder", "")]
                if not any(m in n.lower() for n in names):
                    continue
            out.append({**v, "meeting_date": date, "source_file": filename})
    out.sort(key=lambda v: v["meeting_date"])
    return out
