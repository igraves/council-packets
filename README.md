# pvcouncil — Prairie Village City Council document search

As a sitting councilor, a group of questions would routinely come up during 
meetings and discussions with constituents: "When did this item pass?" "Who voted for it?"
"What were the discussions around it, if any?" 

I made this tool to process and document council packets going back to 2015 (the earliest
ones still available) so I could get some historical context into past council thought
and decision-making processes. The technical bits follow.

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

### Budgets

`pvc extract-budgets` tracks the city budget year over year. Each budget
moves through the packets in stages: the preliminary budget in May/June, the
published hearing notice in July, and the adoption hearing in
August/September. Budget-season packets are scanned for pages about the
coming year's budget, and only those pages go to Claude. Claude returns:

- headline figures: total budget, General Fund, property tax levied, mill
  levy, revenue-neutral rate, assessed valuation, reserves
- General Fund spending by expense category and by department
- the cost drivers and assumptions staff cite, each with its source page

Results go to `budgets.json`. Adopted figures take precedence over proposed
or preliminary ones. Where a packet shows a prior-year column, that column
fills gaps for the earlier year.

```sh
uv run pvc extract-budgets                  # extract new packets (incremental)
uv run pvc extract-budgets --batch --force  # full re-extraction via Batches API

uv run pvc budget                           # headline trend across all years
uv run pvc budget --year 2027               # 2027 vs 2026: what changed and why
uv run pvc budget --compare 2020 2027       # any two years
uv run pvc budget --markdown budget-history.md
```

A comparison shows how the headline figures changed and how much of the
General Fund change each expense category accounts for. It also lists the
largest department changes and the reasons staff gave. Category and department
changes come from a single document's budget and prior-year columns where one
exists, so renamed or regrouped lines don't show up as false changes.
Coverage is thinner before 2019 because the packets from those years carry
only summary budget pages.

An MCP server (`pvcouncil.mcp_server`) exposes `search`, `ask`, `votes`,
`budget`, `list_documents`, and `stats` as tools for MCP clients.

## Data

`pv_council_docs/` (source PDFs) and `chroma_db/` (the vector index) are not
tracked in git. Rebuild them from scratch with `pvc download && pvc index`
(embedding the full corpus costs roughly $0.20 in Voyage API usage).

## License

[AGPL-3.0-or-later](LICENSE)
