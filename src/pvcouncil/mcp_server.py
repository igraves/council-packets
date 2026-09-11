"""MCP server for Prairie Village City Council document search."""

from mcp.server.fastmcp import FastMCP

from pvcouncil import config

mcp = FastMCP(
    "Prairie Village Council Docs",
    instructions=(
        "Search and ask questions about Prairie Village, Kansas city council "
        "documents (meeting packets, minutes, etc.). Use 'search' for finding "
        "specific documents and 'ask' for synthesized answers with citations."
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
        doc_type: Filter by document type: "packet" or "minutes"
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

    lines = [f"{len(results)} results ({mode} search):\n"]
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
        doc_type: Filter by document type: "packet" or "minutes"
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
        doc_type: Filter by type: "packet", "minutes", or "other"
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


if __name__ == "__main__":
    mcp.run()
