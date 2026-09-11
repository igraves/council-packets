# pvcouncil — Prairie Village City Council document search

Semantic and keyword search, plus cited RAG Q&A, over Prairie Village, Kansas
city council packets and minutes (2015–present). PDFs are chunked and embedded
into a local [ChromaDB](https://www.trychroma.com/) index with
[Voyage AI](https://www.voyageai.com/) embeddings; `pvc ask` synthesizes
answers with Claude, citing the source packet, page, and meeting date.

Documents are fetched from the city's public
[CivicClerk portal](https://prairievillageks.portal.civicclerk.com/) API.
Scanned/image-only PDFs are OCR'd with Tesseract.

## Setup

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and optionally
Tesseract (`brew install tesseract`) for OCR of scanned documents.

```sh
cp .env.example .env   # add your Voyage AI and Anthropic API keys
uv sync
```

## Usage

```sh
uv run pvc download    # fetch new packets/minutes from CivicClerk
uv run pvc index       # chunk + embed new documents (incremental)

uv run pvc search "stormwater drainage"
uv run pvc search "ordinance 2428" --keyword
uv run pvc search "housing density" --type packet --after 2023-01-01

uv run pvc ask "What has the council decided about the community center?"

uv run pvc stats
```

### Voting records

`pvc extract-votes` runs every minutes document through Claude with a JSON
schema, producing `votes.json`: each formal motion, its mover/seconder,
result, tally, and — where the minutes explicitly record them (roll calls,
named dissents) — who voted yes/no/abstain. Individual votes are never
inferred from attendance. Extraction is incremental; pass `--batch` to use
the Message Batches API (50% cheaper) for large re-extractions.

```sh
uv run pvc extract-votes                  # extract new minutes (incremental)
uv run pvc extract-votes --batch --force  # full re-extraction via Batches API

uv run pvc votes --result failed --after 2024-01-01
uv run pvc votes --member Reddell --side no
uv run pvc votes --search "community center" --summary
uv run pvc votes --markdown council-votes.md   # regenerate the full report
```

The full voting record is checked in as
[council-votes.md](council-votes.md) — every recorded vote from April 2015
onward, grouped by meeting.

An MCP server (`pvcouncil.mcp_server`) exposes `search`, `ask`, `votes`,
`list_documents`, and `stats` as tools for MCP clients.

## Data

`pv_council_docs/` (source PDFs) and `chroma_db/` (the vector index) are not
tracked in git. Rebuild them from scratch with `pvc download && pvc index`
(embedding the full corpus costs roughly $0.20 in Voyage API usage).

## License

[AGPL-3.0-or-later](LICENSE)
