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
uv run pvc index --ocr-gaps   # re-index docs whose scanned pages OCR missed

uv run pvc search "stormwater drainage"
uv run pvc search "ordinance 2428" --keyword
uv run pvc search "housing density" --type packet --after 2023-01-01

uv run pvc ask "What has the council decided about the community center?"

uv run pvc stats
```

### City reports archive

`pvc download-archive` crawls the
[Archive Center](https://www.prairievillageks.gov/Archive.aspx) on the city
website, a separate CivicPlus site from the CivicClerk meeting portal. It
holds the published reports that never appear in a council packet:

- budget books (2006 onward)
- annual comprehensive financial reports (1997 onward)
- quarterly financial reports and popular annual financial reports
- community improvement district reports
- police annual reports, bias-based policing analyses and stop data summaries

Files go to `pv_council_docs/archive/`, and `archive.json` records each
document's category, title and source URL. Reruns fetch only new
items. These reports have no meeting date, so the filename date comes from
the title:

- a quarterly report gets its quarter's end date
- a budget gets January 1 of its budget year
- other annual reports get December 31 of the year they cover

```sh
uv run pvc download-archive --list          # show categories and their IDs
uv run pvc download-archive                 # download everything new
uv run pvc download-archive --category 37   # one category (Budget)
uv run pvc index                            # then index as usual

uv run pvc search "fund balance policy" --type archive
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

### Parcels and property values

`pvc import-parcels` reads a Johnson County AIMS parcel export (.xlsx) and
writes `parcels/<year>.csv`, one row per tax parcel. The rows include
address, land use, zoning, tax unit, year built, the county's grade and
condition ratings, and appraised and assessed values. The county export
lists condo and townhouse units twice, once for the unit and once for the
shared common area. Import keeps one row per parcel and records the
common-area acreage separately.

Owner names and mailing addresses go to `parcels/<year>-owners.csv`. That
file is gitignored and stays on your machine. The `--owner` and
`--mails-elsewhere` filters only work where it exists. The checked-in CSV
keeps three columns derived from the owner data, none of which says who
the owner is:

- `corp_or_llc_owned`: 1 if an owner name shows a corporation, LLC, or
  partnership: LLC, L.C., Inc., Corp., Company, Ltd., LP, LLP, or
  Partners/Partnership. Trust companies acting as trustee aren't counted.
- `owner_out_of_state`: 1 if the owner's mailing address is outside Kansas,
  including foreign addresses
- `owner_mail_state`: the two-letter state on the owner's mailing address

The `--corp-or-llc`, `--out-of-state`, and `--mail-state` filters use these columns.

Platted parcels also get `subdivision_code`, `subdivision`, `plat`, `block`,
and `lot`. The code and the block and lot come from the county parcel ID:
`OP55000002 0030` is subdivision 5500 (Prairie Village), block 2, lot 30.
The plat name is parsed from the legal description. A subdivision code can
cover several plats, such as West Riding and West Riding 2nd Plat, so the
subdivision takes the name of its most common plat. Unplatted acreage
(`OF…` parcel IDs) has none of these fields.

```sh
uv run pvc import-parcels "2026 Parcel Data.xlsx" --year 2026

uv run pvc parcels --address "4000 W 68TH"            # one parcel's full record
uv run pvc parcels --owner "PRAIRIE VILLAGE" --class Exempt   # needs the local owners file
uv run pvc parcels --use duplex --sort value
uv run pvc parcels --subdivision "West Riding" --summary
uv run pvc parcels --subdivision 5500 --block 2           # by subdivision code
uv run pvc parcels --built-after 2020 --zoning R-1B --summary
uv run pvc parcels --mails-elsewhere --class Residential --summary
uv run pvc parcels --corp-or-llc --use "single family" --summary
uv run pvc parcels --mail-state MO --class Residential
uv run pvc parcels --markdown property-2026.md        # the full report
```

[property-2026.md](property-2026.md) has 2026 totals by class, zoning,
subdivision, and housing type. It also covers single-family values by
decade built, new construction, tax-exempt property by use, tax units, and
the largest parcels. The report names no owners. Its ownership section
gives the shares of corporate- or LLC-owned and out-of-state-owned parcels, broken down by
mailing state and by use. If the local owners file is present, it also adds
the share of tax bills mailed somewhere other than the property. If
`budgets.json` is present, the report also checks the parcel total against
the assessed valuation in the budget.

### Recorded plats (Recorder of Deeds)

`pvc plat-survey` finds the recorded plats for every subdivision in the
parcel data, using the Johnson County Recorder of Deeds subscription site
(TrakRecord Public Access). It needs a subscriber login in `.env`:

```sh
JOCO_RECORDER_USER=...
JOCO_RECORDER_PASS=...
```

`pvc recorder-check` tests the login and one search without printing the
credentials or session values.

The recorder indexes a plat under the plat's own name, and older plats
have no legal description in the index, so the survey runs one name search
per plat name, limited to the PLAT category. It writes:

- `deeds/plats.json`: every recorded plat instrument found, with recorded
  date, book and page, instrument number, and the recorder's record IDs
  (`id`, `docid`) for pulling images later. It records only the plat names,
  not other parties.
- [subdivision-plats.md](subdivision-plats.md): each subdivision's original
  platting date and every plat recorded for it.

Replats are searched under both their full name ("REPLAT OF PART OF LOT 5
MEADOW VIEW") and their parent subdivision's name. A name search is a
prefix match, so it also finds plats with longer names. Each instrument is
marked as one of these:

- `exact`: indexed under one of our plat names
- `variant`: a longer name, such as "MISSION VILLAGE BLOCK 2", placed in
  Prairie Village by its section, township, and range (Township 12 or 13,
  Range 25) or by its subdivision legal
- `prefix`: a longer name that the index gives no location for
- `elsewhere`: located elsewhere in the county

Only `exact` and `variant` count toward a subdivision. The rest are listed
in the report for review. When a plat's place can only be settled by
reading its image, the decision goes in
[deeds/plat_review.json](deeds/plat_review.json), keyed by book and page,
and the survey applies it. Reading the images placed the 1937–1946
COUNTRYSIDE plats in Mission, not Prairie Village.

`pvc plat-images` downloads the plat image PDFs for every Prairie Village
plat in the manifest to `deeds/images/`, which is gitignored. Viewing and
downloading images is free on the subscription; certified copies are not.
Old scans run up to about 9 MB each, so expect a few hundred MB in all.
Reruns skip images already downloaded:

```sh
uv run pvc plat-images --limit 5   # try a few
uv run pvc plat-images             # all of them
```

Raw search results are cached in `deeds/cache/`, which is gitignored
because rows can carry other party names. Reruns reuse the cache:

```sh
uv run pvc plat-survey             # search names not yet cached, rebuild
uv run pvc plat-survey --offline   # rebuild from the cache only
uv run pvc plat-survey --refresh   # search everything again
```

### Restrictions and covenants

Three commands find each subdivision's recorded deed restrictions and
covenants:

```sh
uv run pvc plat-images                  # plat image PDFs → deeds/images/
uv run pvc plat-ocr                     # OCR the plats → deeds/plat_restrictions.json
uv run pvc restriction-survey --walk    # restriction instruments → subdivision-restrictions.md
```

**On the plat.** `pvc plat-ocr` reads every plat image with Tesseract at 0,
90, and 270 degrees, since some plats print their dedication sideways. It
flags restriction language. Some plats carry their covenants in full, such as
the 1923 Ridge View resurvey and the 1937 Sunset Heights View plat. Others
state that restrictions are recorded separately. The full OCR text and the
excerpts stay in `deeds/ocr/`, which is gitignored because plats name their
signers.

**Filed with the plat.** Developers recorded a subdivision's restrictions
right after its plat. The 1941 Prairie Village plat is document 289996, and
the J.C. Nichols Declaration of Restrictions for the same land (MISC 26/349)
is 289997. The survey looks for instruments recorded the same day as a plat
whose document number is within five after the plat's. This is the strongest
evidence of a subdivision's original restrictions.

**Indexed as restrictions.** The recorder labels some instruments
"Declaration of Restrictions" (1914 on) or "Declaration of Home Owners
Association". A document-type search works only with no category selected.
The survey pulls every such instrument county-wide and matches them to our
subdivisions by legal description or by a party named for the subdivision
("KENILWORTH HOMES ASSOCIATION").

**The MISC walk.** Before about 1976, most restrictions were indexed only as
"Miscellaneous". `--walk` pulls every Miscellaneous filing recorded from
three months before to twelve months after each pre-1976 plat. It drops
utility easements and scores the rest on several signals:

- a developer party, such as the J.C. Nichols companies
- an UNKNOWN or PUBLIC grantee
- a subdivision legal
- no lot in the legal
- a recording date within a week of the plat

High scorers are listed as candidates to read. No Miscellaneous filings
are indexed for 1972–74. Twelve months in the walk also come back empty when
fetched again: 1925-01, 1925-02, 1939-02, 1948-02, 1948-04 through 1948-07,
1952-05, 1952-06, 1962-08, and 1962-09. Filings from those months would
have to be found by book and page.

**Reading them.** `pvc restriction-read` downloads the image of each
instrument filed with a plat and OCRs it. It classifies each one as a
restriction declaration, homes association declaration, easement, deed, and
so on. It also notes each declaration's terms: how long it runs, setbacks, use
limits, and any racially restrictive covenant. About 40 of the 1950s
instruments are low-resolution photostat negatives that OCR can't read. Those
were classified by eye from their headings, in `deeds/restriction_review.json`,
which also marks papers filed the same day that concern other land. The results
go to `deeds/restriction_reads.json` and feed the report. Images and OCR text
stay local.

Raw rows are cached in `deeds/cache/restrictions/` because they carry party
names. The checked-in `deeds/restrictions.json` and
[subdivision-restrictions.md](subdivision-restrictions.md) list instruments
by date, book and page, and record ID, with no party names. Pass `--offline`
to rebuild from the cache.

An MCP server (`pvcouncil.mcp_server`) exposes `search`, `ask`, `votes`,
`budget`, `list_documents`, and `stats` as tools for MCP clients. Search, ask and list_documents accept `doc_type="archive"` for the city reports.

## Data

`pv_council_docs/` (source PDFs) and `chroma_db/` (the vector index) are not
tracked in git. Rebuild them from scratch with `pvc download && pvc index`
(embedding the full corpus costs roughly $0.20 in Voyage API usage).

## License

[AGPL-3.0-or-later](LICENSE)
