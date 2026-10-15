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


@cli.command("plat-survey")
@click.option("--refresh", is_flag=True, help="Re-run every search instead of reusing cached results")
@click.option("--offline", is_flag=True, help="Rebuild the manifest and report from cached results only")
@click.option("--markdown", "md_path", default="subdivision-plats.md", show_default=True,
              type=click.Path(), help="Where to write the report")
def plat_survey(refresh, offline, md_path):
    """Find the recorded plats for every subdivision in the parcel data (Recorder of Deeds)."""
    from pathlib import Path

    from pvcouncil import plats
    from pvcouncil.recorder import RecorderError

    names = plats.plat_names()
    if not offline:
        try:
            plats.fetch(names, refresh=refresh, echo=click.echo)
        except RecorderError as e:
            raise click.ClickException(f"{e} (finished searches are cached; rerun to resume)")
    manifest = plats.build(names)
    Path(md_path).write_text(plats.render_markdown(manifest))
    exact = sum(1 for e in manifest["instruments"] if e["match"] == "exact")
    click.echo(f"{len(manifest['instruments'])} plat instruments ({exact} exact name matches) → "
               f"{plats.MANIFEST_PATH.relative_to(plats.PROJECT_ROOT)}, {md_path}")


@cli.command("plat-images")
@click.option("--limit", type=int, help="Download at most N images (for testing)")
def plat_images(limit):
    """Download the plat image PDFs for every Prairie Village plat in deeds/plats.json."""
    from pvcouncil import plats
    from pvcouncil.recorder import RecorderError

    try:
        manifest = plats.load_manifest()
        stats = plats.download_images(manifest, limit=limit, echo=click.echo)
    except (FileNotFoundError, RecorderError) as e:
        raise click.ClickException(f"{e} (downloaded images are kept; rerun to resume)")
    click.echo(f"Downloaded {stats['downloaded']} images ({stats['bytes'] / 1e6:.1f} MB), "
               f"{stats['no_image']} with no image → {plats.IMAGES_DIR.relative_to(plats.PROJECT_ROOT)}/")


@cli.command("plat-ocr")
@click.option("--workers", default=6, show_default=True, help="Parallel OCR processes")
def plat_ocr(workers):
    """OCR the downloaded plat images and find the restrictions they state or cite."""
    from collections import Counter

    from pvcouncil import platocr, plats

    manifest = plats.load_manifest()
    platocr.ocr_all(manifest, workers=workers, echo=click.echo)
    out = platocr.build(manifest)
    rows = out["plats"]
    with_terms = [r for r in rows if r["terms"]]
    cited = [r for r in rows if r["citations"]]
    click.echo(f"{len(rows)} plats scanned: {len(with_terms)} with restriction language, "
               f"{len(cited)} citing recorded instruments ({sum(len(r['citations']) for r in cited)} citations)")
    terms = Counter(t for r in rows for t in r["terms"])
    click.echo("  " + ", ".join(f"{t} {n}" for t, n in terms.most_common()))
    click.echo(f"→ {platocr.RESULTS_PATH.relative_to(plats.PROJECT_ROOT)}; "
               f"excerpts in {platocr.EXCERPTS_PATH.relative_to(plats.PROJECT_ROOT)} (local)")


@cli.command("restriction-read")
@click.option("--workers", default=6, show_default=True, help="Parallel OCR processes")
@click.option("--offline", is_flag=True, help="Skip downloading; OCR and classify what is on disk")
def restriction_read(workers, offline):
    """Download, OCR, and classify the instruments filed with a plat."""
    from collections import Counter

    import json

    from pvcouncil import restrictions, restrictread
    from pvcouncil.recorder import RecorderError

    entries = json.loads(restrictions.RESULTS_PATH.read_text())["filed_with_plat"]
    if not offline:
        try:
            s = restrictread.download(entries, echo=click.echo)
        except RecorderError as e:
            raise click.ClickException(str(e))
        click.echo(f"downloaded {s['downloaded']} ({s['bytes'] / 1e6:.0f} MB), {s['no_image']} without images")
    restrictread.ocr_all(entries, workers=workers, echo=click.echo)
    rows = restrictread.build(entries)["read"]
    click.echo(f"{len(rows)} read: " + ", ".join(f"{k} {n}" for k, n in Counter(r["kind"] for r in rows).most_common()))
    feats = Counter(f for r in rows for f in r["features"])
    click.echo("  " + ", ".join(f"{f} {n}" for f, n in feats.most_common()))
    click.echo(f"→ {restrictread.RESULTS_PATH.relative_to(plats_root())}")


def plats_root():
    from pvcouncil import plats

    return plats.PROJECT_ROOT


@cli.command("deed-viewer")
@click.option("--port", default=8765, show_default=True, help="Port on 127.0.0.1")
def deed_viewer(port):
    """Page through the downloaded plats and restriction instruments in a browser."""
    from pvcouncil import viewer

    viewer.serve(port=port, echo=click.echo)


@cli.command("deed-ocr")
@click.option("--engine", "engine_name", help="tesseract, surya, olmocr, qwen-vl, or gemma4")
@click.option("--key", "keys", multiple=True, help="Document key from the viewer (p:<id> or r:<id>); repeatable")
@click.option("--poor", is_flag=True, help="Every document flagged poor OCR")
@click.option("--invert", type=click.Choice(["auto", "on", "off"]), default="auto", show_default=True,
              help="Invert pages: auto inverts mostly-dark pages (photostat negatives)")
@click.option("--rot", type=click.Choice(["0", "90", "180", "270"]), default="0", show_default=True)
@click.option("--redo", is_flag=True, help="Read again documents this engine has already read")
def deed_ocr(engine_name, keys, poor, invert, rot, redo):
    """Read whole documents with an OCR engine; with no --engine, list the engines.

    Readings are saved locally and compared in `pvc deed-viewer`, where one can be
    adopted as a document's report text after checking it against the image."""
    from pvcouncil import ocrengines, viewer

    engines = ocrengines.available(refresh=True)
    if not engine_name:
        for name, e in engines.items():
            click.echo(f"{name:10} {e['label']:16} {'ready' if e['ok'] else 'not set up: ' + e['setup']}")
        return
    ocrengines.get(engine_name)
    if not engines[engine_name]["ok"]:
        raise click.ClickException(f"{engine_name} isn't set up: {engines[engine_name]['setup']}")
    docs = viewer.build_index()
    todo = [docs[k] for k in keys if k in docs] + [d for d in docs.values() if poor and d["ocr"] == "poor"]
    todo = list({d["key"]: d for d in todo}.values())
    if not redo:
        todo = [d for d in todo if not ocrengines.reading_path(engine_name, d["key"]).exists()]
    if not todo:
        raise click.ClickException("nothing to read: pass --key or --poor (and --redo to read again)")
    click.echo(f"Reading {len(todo)} documents ({sum(d['pages'] for d in todo)} pages) with {engine_name}")
    for i, d in enumerate(todo, 1):
        r = ocrengines.read_document(engine_name, d["key"], d["path"], int(rot), invert)
        click.echo(f"[{i}/{len(todo)}] {d['book_page']} ({d['key']}): {r['words']} words "
                   f"(Tesseract {d['ocr_words']}) in {r['seconds']:.0f}s")


@cli.command("restriction-survey")
@click.option("--offline", is_flag=True, help="Rebuild from cached searches only")
@click.option("--walk", is_flag=True, help="Also pull 'Miscellaneous' filings around each pre-1976 plat date")
def restriction_survey(offline, walk):
    """Find recorded restrictions and covenants for the subdivisions (Recorder of Deeds)."""
    from collections import Counter

    from pvcouncil import restrictions
    from pvcouncil.recorder import RecorderError

    if not offline:
        try:
            restrictions.fetch_typed(echo=click.echo)
            restrictions.fetch_cited(echo=click.echo)
            if walk:
                restrictions.fetch_misc_walk(echo=click.echo)
        except RecorderError as e:
            raise click.ClickException(f"{e} (finished searches are cached; rerun to resume)")
    out = restrictions.build()
    from pathlib import Path
    Path("subdivision-restrictions.md").write_text(
        restrictions.render_markdown(out, restrictions.plats.load_manifest()))
    click.echo(f"{len(out['filed_with_plat'])} instruments filed with a plat; "
               f"{len(out.get('misc_candidates', []))} Miscellaneous candidates")
    m = out["instruments"]
    subs = {c for e in m for c in e["subdivision_codes"]}
    click.echo(f"Searched {sum(out['searched'].values()):,} restriction-type instruments county-wide; "
               f"{len(m)} matched to {len(subs)} Prairie Village subdivisions")
    click.echo("  " + ", ".join(f"{t} {n}" for t, n in Counter(e['doc_type'] for e in m).most_common()))
    click.echo(f"→ {restrictions.RESULTS_PATH.relative_to(restrictions.plats.PROJECT_ROOT)}")


@cli.command("recorder-check")
def recorder_check():
    """Test the Recorder of Deeds login and search (prints no credentials or session values)."""
    from pvcouncil.recorder import RecorderError, check

    try:
        report = check()
    except RecorderError as e:
        raise click.ClickException(str(e))
    for k, v in report.items():
        click.echo(f"{k}: {v}")


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
