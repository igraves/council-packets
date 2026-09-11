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
