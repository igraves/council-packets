"""Search: keyword, semantic, hybrid, with metadata filters."""

from dataclasses import dataclass

from pvcouncil.index import get_collection
from pvcouncil.config import DEFAULT_N_RESULTS


@dataclass
class SearchResult:
    chunk_id: str
    text: str
    score: float
    meeting_date: str
    doc_type: str
    source_file: str
    page_number: int


def _date_to_int(date_str: str) -> int:
    """Convert YYYY-MM-DD to integer YYYYMMDD for numeric comparison."""
    return int(date_str.replace("-", ""))


def _build_where(
    doc_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict | None:
    """Build ChromaDB where filter for metadata.

    Uses meeting_date_int (numeric YYYYMMDD) for date range filtering.
    Falls back to client-side filtering if the field doesn't exist.
    """
    conditions = []
    if doc_type:
        conditions.append({"doc_type": {"$eq": doc_type}})
    if date_from:
        conditions.append({"meeting_date_int": {"$gte": _date_to_int(date_from)}})
    if date_to:
        conditions.append({"meeting_date_int": {"$lte": _date_to_int(date_to)}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _query_to_results(result: dict) -> list[SearchResult]:
    """Convert ChromaDB query() result to SearchResult list."""
    results = []
    if not result["ids"] or not result["ids"][0]:
        return results

    for i, chunk_id in enumerate(result["ids"][0]):
        meta = result["metadatas"][0][i]
        dist = result["distances"][0][i] if result.get("distances") else 0.0
        results.append(SearchResult(
            chunk_id=chunk_id,
            text=result["documents"][0][i],
            score=dist,
            meeting_date=meta["meeting_date"],
            doc_type=meta["doc_type"],
            source_file=meta["source_file"],
            page_number=meta["page_number"],
        ))
    return results


def _get_to_results(result: dict) -> list[SearchResult]:
    """Convert ChromaDB get() result to SearchResult list."""
    results = []
    if not result["ids"]:
        return results

    for i, chunk_id in enumerate(result["ids"]):
        meta = result["metadatas"][i]
        results.append(SearchResult(
            chunk_id=chunk_id,
            text=result["documents"][i],
            score=0.0,
            meeting_date=meta["meeting_date"],
            doc_type=meta["doc_type"],
            source_file=meta["source_file"],
            page_number=meta["page_number"],
        ))
    return results


def semantic_search(
    query: str,
    n: int = DEFAULT_N_RESULTS,
    doc_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[SearchResult]:
    """Vector similarity search via Voyage embeddings."""
    collection = get_collection()
    where = _build_where(doc_type, date_from, date_to)
    result = collection.query(
        query_texts=[query],
        n_results=n,
        where=where,
        include=["documents", "metadatas", "distances"],
    )
    return _query_to_results(result)


def keyword_search(
    keyword: str,
    n: int = DEFAULT_N_RESULTS,
    doc_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[SearchResult]:
    """Full-text keyword search via ChromaDB where_document $contains."""
    collection = get_collection()
    where = _build_where(doc_type, date_from, date_to)
    result = collection.get(
        where=where,
        where_document={"$contains": keyword},
        include=["documents", "metadatas"],
        limit=n,
    )
    return _get_to_results(result)


def hybrid_search(
    query: str,
    n: int = DEFAULT_N_RESULTS,
    keyword: str | None = None,
    doc_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[SearchResult]:
    """Semantic search with optional keyword pre-filter.

    If keyword is provided, uses where_document to require it,
    then ranks by semantic similarity.
    """
    collection = get_collection()
    where = _build_where(doc_type, date_from, date_to)
    where_doc = {"$contains": keyword} if keyword else None

    result = collection.query(
        query_texts=[query],
        n_results=n,
        where=where,
        where_document=where_doc,
        include=["documents", "metadatas", "distances"],
    )
    return _query_to_results(result)
