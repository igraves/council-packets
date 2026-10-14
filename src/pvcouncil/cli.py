"""CLI for Prairie Village City Council document search."""

import click

from pvcouncil import config


@click.group()
def cli():
    """Prairie Village City Council document search."""
    pass


@cli.command()
@click.option("--after", "after_str", help="Download meetings after this date (YYYY-MM-DD); default: newest on disk")
@click.option("--dry-run", is_flag=True, help="Show what would be downloaded without downloading")
def download(after_str, dry_run):
    """Download new council documents from the CivicClerk portal."""
    from datetime import date

    from pvcouncil.download import download_new

    after = date.fromisoformat(after_str) if after_str else None

    def progress(event_date, filename, status):
        click.echo(f"  [{event_date}] {filename} — {status}")

    stats = download_new(after=after, dry_run=dry_run, progress_fn=progress)
    click.echo(f"\nDone: {stats['events']} events checked, "
               f"{stats['downloaded']} downloaded, {stats['skipped']} already present")
    if stats["no_files"]:
        click.echo(f"  ({stats['no_files']} events had no published files yet)")
    if stats["downloaded"] and not dry_run:
        click.echo("Run `pvc index` to add the new documents to the search index.")


@cli.command("download-archive")
@click.option("--category", "amids", type=int, multiple=True,
              help="Archive category ID (AMID) to crawl; repeatable. Default: all")
@click.option("--list", "list_only", is_flag=True, help="List archive categories and exit")
@click.option("--dry-run", is_flag=True, help="Show what would be downloaded without downloading")
def download_archive_cmd(amids, list_only, dry_run):
    """Download reports from the city website's Archive Center (budgets, ACFRs, etc.)."""
    from pvcouncil.archive import download_archive, fetch_categories

    if list_only:
        for amid, name in fetch_categories().items():
            click.echo(f"  {amid:4d}  {name}")
        return

    def progress(category, filename, status):
        click.echo(f"  [{category}] {filename} — {status}")

    stats = download_archive(amids=list(amids) or None, dry_run=dry_run, progress_fn=progress)
    click.echo(f"\nDone: {stats['categories']} categories, {stats['listed']} documents listed, "
               f"{stats['downloaded']} downloaded, {stats['skipped']} already present")
    if stats["errors"]:
        click.echo(f"  ({stats['errors']} failed; rerun to retry)")
    if stats["downloaded"] and not dry_run:
        click.echo("Run `pvc index` to add the new documents to the search index.")


@cli.command()
@click.option("--force", is_flag=True, help="Re-index all documents")
@click.option("--ocr-gaps", is_flag=True,
              help="Re-index only documents with scanned pages missed by OCR")
def index(force, ocr_gaps):
    """Index PDF documents into ChromaDB."""
    from pvcouncil.index import index_all, discover_docs
    from pvcouncil.extract import has_ocr_gap

    def progress(current, total, filename, status):
        click.echo(f"  [{current}/{total}] {filename} — {status}")

    click.echo(f"Indexing documents from {config.DOCS_DIR}...")
    if force:
        click.echo("(force mode: re-indexing all documents)")

    only = None
    if ocr_gaps:
        only = {d["filename"] for d in discover_docs() if has_ocr_gap(d["path"])}
        click.echo(f"(re-indexing {len(only)} documents with un-OCR'd scanned pages)")

    stats = index_all(force=force, progress_fn=progress, only=only)

    click.echo(f"\nDone:")
    click.echo(f"  Indexed:  {stats['indexed']}")
    click.echo(f"  Skipped:  {stats['skipped']}")
    click.echo(f"  Errors:   {stats['errors']}")
    click.echo(f"  Chunks:   {stats['chunks_added']}")
    if stats["empty_pages"]:
        click.echo(f"  Empty (no text): {stats['empty_pages']}")


@cli.command()
@click.argument("query")
@click.option("-n", "--num-results", default=10, help="Number of results")
@click.option("--type", "doc_type", type=click.Choice(["packet", "minutes", "archive", "other"]))
@click.option("--after", "date_from", help="After date (YYYY-MM-DD)")
@click.option("--before", "date_to", help="Before date (YYYY-MM-DD)")
@click.option("--keyword", is_flag=True, help="Keyword search instead of semantic")
def search(query, num_results, doc_type, date_from, date_to, keyword):
    """Search council documents (semantic by default, --keyword for exact match)."""
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
        click.echo("No results found.")
        return

    click.echo(f"{len(results)} results ({mode} search):\n")
    for i, r in enumerate(results):
        click.echo(f"{'='*70}")
        click.echo(f"[{i+1}] {r.source_file}")
        click.echo(f"    Date: {r.meeting_date}  |  Type: {r.doc_type}  |  Page: {r.page_number}")
        if r.score:
            click.echo(f"    Score: {r.score:.4f}")
        click.echo()
        # Show first 400 chars of text
        preview = r.text[:400].replace("\n", "\n    ")
        click.echo(f"    {preview}...")
        click.echo()


@cli.command()
@click.argument("question")
@click.option("--type", "doc_type", type=click.Choice(["packet", "minutes", "archive", "other"]))
@click.option("--after", "date_from", help="After date (YYYY-MM-DD)")
@click.option("--before", "date_to", help="Before date (YYYY-MM-DD)")
@click.option("--chunks", "n_chunks", default=config.RAG_N_CHUNKS, help="Number of context chunks")
def ask(question, doc_type, date_from, date_to, n_chunks):
    """Ask a question (RAG synthesis with Claude)."""
    from pvcouncil.rag import ask as rag_ask

    click.echo("Searching and synthesizing...\n")
    result = rag_ask(
        question, n_chunks=n_chunks,
        doc_type=doc_type, date_from=date_from, date_to=date_to,
    )

    click.echo(result["answer"])
    click.echo(f"\n{'─'*70}")
    click.echo(f"Sources ({result['chunks_used']} chunks retrieved):")
    for s in result["sources"]:
        click.echo(f"  {s['date']}  |  {s['doc_type']}  |  {s['file']}")


@cli.command("extract-votes")
@click.option("--force", is_flag=True, help="Re-extract all minutes")
@click.option("--limit", type=int, help="Process at most N documents (for testing)")
@click.option("--workers", default=4, help="Concurrent extraction requests")
@click.option("--batch", "use_batch", is_flag=True,
              help="Use the Message Batches API (50% cheaper, async — best for large re-extractions)")
def extract_votes(force, limit, workers, use_batch):
    """Extract voting records from council minutes (LLM extraction into votes.json)."""
    from pvcouncil.votes import extract_all, extract_all_batch

    if use_batch:
        stats = extract_all_batch(force=force, limit=limit,
                                  progress_fn=lambda msg: click.echo(f"  {msg}"))
    else:
        def progress(current, total, filename, status):
            click.echo(f"  [{current}/{total}] {filename} — {status}")

        stats = extract_all(force=force, limit=limit, workers=workers, progress_fn=progress)
    click.echo(f"\nDone:")
    click.echo(f"  Extracted: {stats['extracted']} documents")
    click.echo(f"  Votes:     {stats['votes_found']}")
    click.echo(f"  Skipped:   {stats['skipped']} (already extracted)")
    click.echo(f"  Errors:    {stats['errors']}")


@cli.command()
@click.option("--member", help="Only votes involving this member (name substring)")
@click.option("--side", type=click.Choice(["yes", "no", "abstain"]),
              help="With --member: only votes where they voted this way")
@click.option("--result", type=click.Choice(["passed", "failed", "tabled", "withdrawn", "unknown"]))
@click.option("--search", help="Substring match on the vote item")
@click.option("--after", "date_from", help="On or after date (YYYY-MM-DD)")
@click.option("--before", "date_to", help="On or before date (YYYY-MM-DD)")
@click.option("--summary", is_flag=True, help="Per-member summary instead of listing votes")
@click.option("--markdown", "md_path", type=click.Path(), help="Write matched votes to a Markdown file")
@click.option("-n", "--num", default=50, help="Max votes to list (default 50; 0 = all)")
def votes(member, side, result, search, date_from, date_to, summary, md_path, num):
    """Query extracted council voting records."""
    from collections import Counter

    from pvcouncil.votes import VOTES_PATH, query_votes, render_markdown

    if not VOTES_PATH.exists():
        click.echo("No votes.json yet — run `pvc extract-votes` first.")
        return

    records = query_votes(member=member, side=side, result=result,
                          search=search, date_from=date_from, date_to=date_to)
    if not records:
        click.echo("No matching votes.")
        return

    if md_path:
        from pathlib import Path
        Path(md_path).write_text(render_markdown(records))
        click.echo(f"Wrote {len(records)} votes to {md_path}")
        return

    if summary:
        yes_c, no_c, abstain_c = Counter(), Counter(), Counter()
        for v in records:
            yes_c.update(v["yes"])
            no_c.update(v["no"])
            abstain_c.update(v["abstain"])
        click.echo(f"{len(records)} votes matched. Named-vote counts per member "
                   f"(roll calls and recorded dissents only):\n")
        members = sorted(set(yes_c) | set(no_c) | set(abstain_c))
        width = max((len(m) for m in members), default=10)
        click.echo(f"  {'member':<{width}}  {'yes':>5} {'no':>5} {'abstain':>8}")
        for m in members:
            click.echo(f"  {m:<{width}}  {yes_c[m]:>5} {no_c[m]:>5} {abstain_c[m]:>8}")
        return

    shown = records if num == 0 else records[-num:]
    for v in shown:
        flag = {"passed": "PASSED", "failed": "FAILED"}.get(v["result"], v["result"].upper())
        tally = f" {v['tally']}" if v["tally"] else ""
        click.echo(f"{v['meeting_date']}  {flag}{tally}  [{v['motion_type']}] {v['item']}")
        if v["no"]:
            click.echo(f"    No: {', '.join(v['no'])}")
        if v["abstain"]:
            click.echo(f"    Abstain: {', '.join(v['abstain'])}")
        if v["yes"]:
            click.echo(f"    Yes: {', '.join(v['yes'])}")
    click.echo(f"\n{len(records)} votes matched"
               + (f" (showing last {len(shown)})" if len(shown) < len(records) else ""))


@cli.command("extract-budgets")
@click.option("--force", is_flag=True, help="Re-extract all budget documents")
@click.option("--limit", type=int, help="Process at most N documents (for testing)")
@click.option("--workers", default=4, help="Concurrent extraction requests")
@click.option("--batch", "use_batch", is_flag=True,
              help="Use the Message Batches API (50% cheaper, async — best for large re-extractions)")
@click.option("--source", type=click.Choice(["all", "packet", "archive"]), default="all",
              help="Council packets, Archive Center budget books, or both (default)")
def extract_budgets(force, limit, workers, use_batch, source):
    """Extract annual budget figures and cost drivers from packets and budget books (into budgets.json)."""
    from pvcouncil.budget import extract_all, extract_all_batch

    sources = {"packet", "archive"} if source == "all" else {source}
    if use_batch:
        stats = extract_all_batch(force=force, limit=limit, sources=sources,
                                  progress_fn=lambda msg: click.echo(f"  {msg}"))
    else:
        def progress(current, total, filename, status):
            click.echo(f"  [{current}/{total}] {filename} — {status}")

        stats = extract_all(force=force, limit=limit, workers=workers, sources=sources,
                            progress_fn=progress)
    click.echo(f"\nDone:")
    click.echo(f"  Extracted: {stats['extracted']} documents")
    click.echo(f"  Skipped:   {stats['skipped']} (already extracted or no budget pages)")
    click.echo(f"  Errors:    {stats['errors']}")


@cli.command()
@click.option("--year", type=int, help="Compare this budget year with the year before")
@click.option("--compare", "years", nargs=2, type=int, help="Compare two budget years, e.g. --compare 2020 2027")
@click.option("--markdown", "md_path", type=click.Path(), help="Write the full budget history to a Markdown file")
def budget(year, years, md_path):
    """Track budgets year over year: headline trend, changes, and cost drivers."""
    from pvcouncil.budget import (BUDGETS_PATH, compare, render_compare,
                                  render_markdown, render_trend)

    if not BUDGETS_PATH.exists():
        click.echo("No budgets.json yet — run `pvc extract-budgets` first.")
        return

    if md_path:
        from pathlib import Path
        Path(md_path).write_text(render_markdown())
        click.echo(f"Wrote budget history to {md_path}")
        return

    if year or years:
        a, b = years if years else (year - 1, year)
        try:
            click.echo(render_compare(compare(a, b)))
        except KeyError as e:
            raise click.ClickException(e.args[0])
        return

    click.echo(render_trend())


@cli.command("import-parcels")
@click.argument("xlsx_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--year", type=int, required=True, help="Valuation year of the export")
@click.option("--sheet", help="Worksheet to read (default: the one with the most rows)")
def import_parcels(xlsx_path, year, sheet):
    """Import a Johnson County AIMS parcel export (.xlsx) into parcels/<year>.csv."""
    from pathlib import Path

    from pvcouncil.parcels import import_xlsx

    try:
        stats = import_xlsx(Path(xlsx_path), year, sheet)
    except (KeyError, ValueError) as e:
        raise click.ClickException(str(e))
    click.echo(f"Sheet {stats['sheet']!r}: {stats['rows']:,} polygon rows → {stats['parcels']:,} tax parcels")
    if stats["skipped"]:
        click.echo(f"  Skipped {stats['skipped']} rows with no parcel ID (summary rows)")
    if stats["conflicts"]:
        click.echo(f"  Warning: {len(stats['conflicts'])} parcels have rows with differing values: "
                   + ", ".join(stats["conflicts"][:5]))
    click.echo(f"Wrote {stats['path']}")


@cli.command()
@click.option("--year", type=int, help="Valuation year (default: latest imported)")
@click.option("--address", help="Substring match on the property address")
@click.option("--owner", help="Substring match on owner or mailing name")
@click.option("--zoning", help="Zoning district, e.g. R-1A")
@click.option("--class", "cls", help="County class: Residential, Commercial, Exempt, Vacant, ...")
@click.option("--use", help="Substring match on land use, e.g. duplex, townhouse, office")
@click.option("--tax-unit", help="Tax unit code, e.g. 0897UT")
@click.option("--subdivision", help="Subdivision or plat name (substring), or 4-digit subdivision code")
@click.option("--block", help="Block within the subdivision")
@click.option("--built-after", type=int, help="Year built on or after")
@click.option("--built-before", type=int, help="Year built on or before")
@click.option("--min-value", type=int, help="Minimum appraised value")
@click.option("--max-value", type=int, help="Maximum appraised value")
@click.option("--mails-elsewhere", is_flag=True, help="Only parcels whose tax bill goes to another address")
@click.option("--corp-or-llc", is_flag=True, help="Only parcels that appear to be owned by a corporation, LLC, or partnership")
@click.option("--out-of-state", is_flag=True, help="Only parcels whose owner's mailing address is outside Kansas")
@click.option("--mail-state", help="Owner's mailing state, e.g. MO")
@click.option("--sort", type=click.Choice(["value", "assessed", "year", "address"]), default="address")
@click.option("--summary", is_flag=True, help="Totals and breakdowns for the matched parcels")
@click.option("--markdown", "md_path", type=click.Path(), help="Write the full property report for the year")
@click.option("-n", "--num", default=50, help="Max parcels to list (default 50; 0 = all)")
def parcels(year, address, owner, zoning, cls, use, tax_unit, subdivision, block, built_after, built_before,
            min_value, max_value, mails_elsewhere, corp_or_llc, out_of_state, mail_state, sort, summary, md_path, num):
    """Look up parcels and property values, or summarize them."""
    from pvcouncil import parcels as pm

    try:
        records = pm.load(year)
    except FileNotFoundError as e:
        raise click.ClickException(str(e))

    if md_path:
        from pathlib import Path
        Path(md_path).write_text(pm.render_markdown(year))
        click.echo(f"Wrote property report to {md_path}")
        return

    if (owner or mails_elsewhere) and not pm.has_owners(records):
        raise click.ClickException(
            "Owner data isn't in the repo. Run `pvc import-parcels` on the county export "
            "to create the local parcels/<year>-owners.csv.")

    matched = pm.query(records, address=address, owner=owner, zoning=zoning, cls=cls, use=use,
                       tax_unit=tax_unit, subdivision=subdivision, block=block,
                       built_after=built_after, built_before=built_before,
                       min_value=min_value, max_value=max_value, mails_elsewhere_only=mails_elsewhere,
                       corp_or_llc=corp_or_llc, out_of_state=out_of_state, mail_state=mail_state)
    if not matched:
        click.echo("No matching parcels.")
        return
    if summary:
        click.echo(pm.render_summary(matched))
        return
    if len(matched) == 1:
        click.echo(pm.render_detail(matched[0]))
        return

    sort_key = {"value": lambda p: -(p["total_value"] or 0),
                "assessed": lambda p: -(p["assessed_total"] or 0),
                "year": lambda p: -(p["year_built"] or 0),
                "address": lambda p: p["address"] or ""}[sort]
    matched.sort(key=sort_key)
    click.echo(f"{len(matched):,} parcels\n")
    click.echo(pm.render_list(matched, num))


@cli.command()
def stats():
    """Show index statistics."""
    from pvcouncil.index import get_collection, discover_docs

    collection = get_collection()
    count = collection.count()
    docs = discover_docs()

    click.echo(f"Collection:   {config.COLLECTION_NAME}")
    click.echo(f"Chunks:       {count}")
    click.echo(f"PDFs on disk: {len(docs)}")
    click.echo(f"ChromaDB dir: {config.CHROMA_DIR}")


@cli.command("update-metadata")
def update_metadata():
    """Add meeting_date_int to existing chunks (no re-embedding needed)."""
    from pvcouncil.index import get_collection

    collection = get_collection(with_embeddings=False)
    total = collection.count()
    click.echo(f"Patching {total} chunks with meeting_date_int...")

    batch_size = 500
    updated = 0
    for offset in range(0, total, batch_size):
        batch = collection.get(
            limit=batch_size,
            offset=offset,
            include=["metadatas"],
        )
        ids_to_update = []
        metas_to_update = []
        for chunk_id, meta in zip(batch["ids"], batch["metadatas"]):
            if "meeting_date_int" not in meta:
                date_str = meta.get("meeting_date", "unknown")
                date_int = int(date_str.replace("-", "")) if date_str != "unknown" else 0
                meta["meeting_date_int"] = date_int
                ids_to_update.append(chunk_id)
                metas_to_update.append(meta)

        if ids_to_update:
            collection.update(ids=ids_to_update, metadatas=metas_to_update)
            updated += len(ids_to_update)

        click.echo(f"  {min(offset + batch_size, total)}/{total} checked, {updated} patched")

    click.echo(f"Done: {updated} chunks patched with meeting_date_int")


if __name__ == "__main__":
    cli()
