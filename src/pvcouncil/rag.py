"""RAG: retrieve chunks, synthesize answer with Claude."""

import anthropic

from pvcouncil.config import ANTHROPIC_API_KEY, CLAUDE_MODEL, RAG_N_CHUNKS
from pvcouncil.search import hybrid_search


SYSTEM_PROMPT = (
    "You are a research assistant for Prairie Village, Kansas city council documents. "
    "Answer questions based ONLY on the provided document excerpts. "
    "Cite sources using [Source N] notation. "
    "If the documents don't contain enough information, say so clearly. "
    "Be specific about dates, votes, and decisions."
)


def ask(
    question: str,
    n_chunks: int = RAG_N_CHUNKS,
    doc_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Ask a question, get a synthesized answer with citations.

    Returns {answer, sources, chunks_used}
    """
    results = hybrid_search(
        query=question,
        n=n_chunks,
        doc_type=doc_type,
        date_from=date_from,
        date_to=date_to,
    )

    if not results:
        return {
            "answer": "No relevant documents found for this query.",
            "sources": [],
            "chunks_used": 0,
        }

    # Format context block
    context_parts = []
    for i, r in enumerate(results):
        context_parts.append(
            f"[Source {i+1}] {r.source_file} (page {r.page_number}, {r.meeting_date})\n"
            f"{r.text}"
        )
    context = "\n\n---\n\n".join(context_parts)

    user_message = (
        f"Based on these Prairie Village city council document excerpts:\n\n"
        f"{context}\n\n"
        f"Question: {question}\n\n"
        f"Answer with specific citations to the sources above."
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    answer_text = response.content[0].text

    # Deduplicate sources
    sources = []
    seen = set()
    for r in results:
        key = (r.source_file, r.meeting_date)
        if key not in seen:
            seen.add(key)
            sources.append({
                "file": r.source_file,
                "date": r.meeting_date,
                "doc_type": r.doc_type,
            })

    return {
        "answer": answer_text,
        "sources": sources,
        "chunks_used": len(results),
    }
