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


@cli.command()
@click.option("--force", is_flag=True, help="Re-index all documents")
def index(force):
    """Index PDF documents into ChromaDB."""
    from pvcouncil.index import index_all

    def progress(current, total, filename, status):
        click.echo(f"  [{current}/{total}] {filename} — {status}")

    click.echo(f"Indexing documents from {config.DOCS_DIR}...")
    if force:
        click.echo("(force mode: re-indexing all documents)")

    stats = index_all(force=force, progress_fn=progress)

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
@click.option("--type", "doc_type", type=click.Choice(["packet", "minutes"]))
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
@click.option("--type", "doc_type", type=click.Choice(["packet", "minutes"]))
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
