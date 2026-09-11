"""Text -> sized chunks with metadata."""

from dataclasses import dataclass

from pvcouncil.config import CHUNK_SIZE_TOKENS, CHUNK_OVERLAP_TOKENS, CHARS_PER_TOKEN


@dataclass
class Chunk:
    text: str
    page_number: int
    chunk_index: int
    char_start: int
    approx_tokens: int


def chunk_pages(
    pages: list[tuple[int, str]],
    chunk_size: int = CHUNK_SIZE_TOKENS,
    overlap: int = CHUNK_OVERLAP_TOKENS,
) -> list[Chunk]:
    """Split page text into overlapping chunks.

    Splits on paragraph boundaries (double newline), accumulates until
    ~chunk_size tokens, then starts a new chunk with overlap from the
    end of the previous one.
    """
    target_chars = chunk_size * CHARS_PER_TOKEN
    overlap_chars = overlap * CHARS_PER_TOKEN

    # Build full text and page offset map
    full_text = ""
    page_starts: list[tuple[int, int]] = []  # (char_offset, page_number)
    for page_num, text in pages:
        page_starts.append((len(full_text), page_num))
        full_text += text + "\n\n"

    if not full_text.strip():
        return []

    # Split into paragraphs
    paragraphs = [p.strip() for p in full_text.split("\n\n") if p.strip()]

    chunks: list[Chunk] = []
    current_text = ""
    current_start = 0
    char_pos = 0

    for para in paragraphs:
        if len(current_text) + len(para) > target_chars and current_text:
            # Emit chunk
            page = _page_at_offset(page_starts, current_start)
            chunks.append(Chunk(
                text=current_text.strip(),
                page_number=page,
                chunk_index=len(chunks),
                char_start=current_start,
                approx_tokens=len(current_text) // CHARS_PER_TOKEN,
            ))
            # Overlap: start next chunk with tail of current
            if len(current_text) > overlap_chars:
                overlap_text = current_text[-overlap_chars:]
            else:
                overlap_text = ""
            current_start = char_pos - len(overlap_text)
            current_text = overlap_text

        current_text += para + "\n\n"
        char_pos += len(para) + 2

    # Final chunk
    if current_text.strip():
        page = _page_at_offset(page_starts, current_start)
        chunks.append(Chunk(
            text=current_text.strip(),
            page_number=page,
            chunk_index=len(chunks),
            char_start=current_start,
            approx_tokens=len(current_text) // CHARS_PER_TOKEN,
        ))

    return chunks


def _page_at_offset(page_starts: list[tuple[int, int]], offset: int) -> int:
    """Find which page a character offset falls on."""
    page = page_starts[0][1]
    for start, pnum in page_starts:
        if start > offset:
            break
        page = pnum
    return page
