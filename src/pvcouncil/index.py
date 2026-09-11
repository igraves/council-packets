"""ChromaDB ingestion with incremental tracking."""

import hashlib
import json
import time
from pathlib import Path

import chromadb
from chromadb.utils.embedding_functions import VoyageAIEmbeddingFunction

from pvcouncil.config import (
    CHROMA_DIR, COLLECTION_NAME, VOYAGE_API_KEY, EMBEDDING_MODEL, DOCS_DIR,
)
from pvcouncil.extract import extract_pages
from pvcouncil.chunk import chunk_pages


def get_collection(with_embeddings: bool = True) -> chromadb.Collection:
    """Get or create the ChromaDB collection.

    Set with_embeddings=False for metadata-only operations (no API key needed).
    """
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    if with_embeddings:
        embedding_fn = VoyageAIEmbeddingFunction(
            api_key=VOYAGE_API_KEY,
            model_name=EMBEDDING_MODEL,
        )
    else:
        embedding_fn = None
    return client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=embedding_fn,
        metadata={"hnsw:space": "cosine"},
    )


def _file_hash(path: Path) -> str:
    """Quick hash for incremental tracking: size + mtime."""
    stat = path.stat()
    return f"{stat.st_size}_{int(stat.st_mtime)}"


def _chunk_id(doc_type: str, date: str, filename: str, chunk_idx: int) -> str:
    """Deterministic chunk ID."""
    name_hash = hashlib.md5(filename.encode()).hexdigest()[:8]
    return f"{doc_type}_{date}_{name_hash}_{chunk_idx:04d}"


def discover_docs() -> list[dict]:
    """Walk corpus directories, return list of doc descriptors.

    Each: {path, doc_type, meeting_date, filename}
    Derives doc_type from subdirectory, meeting_date from filename prefix.
    """
    docs = []
    type_map = {"packets": "packet", "minutes": "minutes", "other": "other"}

    for subdir, doc_type in type_map.items():
        dir_path = DOCS_DIR / subdir
        if not dir_path.exists():
            continue
        for f in sorted(dir_path.iterdir()):
            if f.suffix.lower() not in (".pdf",):
                continue
            # Extract date from YYYY-MM-DD prefix
            date = f.name[:10] if len(f.name) >= 10 and f.name[4] == "-" else "unknown"
            docs.append({
                "path": f,
                "doc_type": doc_type,
                "meeting_date": date,
                "filename": f.name,
            })
    return docs


def index_all(force: bool = False, batch_size: int = 50, progress_fn=None) -> dict:
    """Index all documents into ChromaDB.

    Incremental: skips docs whose file hash matches what's already stored.
    Returns stats: {total, indexed, skipped, errors, chunks_added, empty_pages}
    """
    collection = get_collection()
    tracker_path = CHROMA_DIR / "indexed_files.json"

    CHROMA_DIR.mkdir(parents=True, exist_ok=True)

    if tracker_path.exists() and not force:
        tracked = json.loads(tracker_path.read_text())
    else:
        tracked = {}

    docs = discover_docs()
    stats = {
        "total": len(docs), "indexed": 0, "skipped": 0,
        "errors": 0, "chunks_added": 0, "empty_pages": 0,
    }

    for doc_idx, doc in enumerate(docs):
        file_hash = _file_hash(doc["path"])
        track_key = str(doc["path"])

        if not force and tracked.get(track_key) == file_hash:
            stats["skipped"] += 1
            if progress_fn:
                progress_fn(doc_idx + 1, len(docs), doc["filename"], "skipped")
            continue

        try:
            pages = extract_pages(doc["path"])
            if not pages:
                stats["skipped"] += 1
                stats["empty_pages"] += 1
                if progress_fn:
                    progress_fn(doc_idx + 1, len(docs), doc["filename"], "empty")
                continue

            chunks = chunk_pages(pages)
            if not chunks:
                stats["skipped"] += 1
                if progress_fn:
                    progress_fn(doc_idx + 1, len(docs), doc["filename"], "no_chunks")
                continue

            # Build batch for upsert
            ids = []
            documents = []
            metadatas = []

            for chunk in chunks:
                chunk_id = _chunk_id(
                    doc["doc_type"], doc["meeting_date"],
                    doc["filename"], chunk.chunk_index,
                )
                ids.append(chunk_id)
                documents.append(chunk.text)
                # meeting_date_int enables numeric $gte/$lte filtering in ChromaDB
                date_int = int(doc["meeting_date"].replace("-", "")) if doc["meeting_date"] != "unknown" else 0
                metadatas.append({
                    "meeting_date": doc["meeting_date"],
                    "meeting_date_int": date_int,
                    "doc_type": doc["doc_type"],
                    "page_number": chunk.page_number,
                    "source_file": doc["filename"],
                    "chunk_index": chunk.chunk_index,
                })

            # Upsert in batches (respect Voyage rate limits)
            for i in range(0, len(ids), batch_size):
                end = i + batch_size
                collection.upsert(
                    ids=ids[i:end],
                    documents=documents[i:end],
                    metadatas=metadatas[i:end],
                )
                if end < len(ids):
                    time.sleep(0.1)

            tracked[track_key] = file_hash
            stats["indexed"] += 1
            stats["chunks_added"] += len(chunks)

            if progress_fn:
                progress_fn(doc_idx + 1, len(docs), doc["filename"], f"{len(chunks)} chunks")

        except Exception as e:
            stats["errors"] += 1
            if progress_fn:
                progress_fn(doc_idx + 1, len(docs), doc["filename"], f"ERROR: {e}")

    # Save tracking state
    tracker_path.write_text(json.dumps(tracked, indent=2))

    return stats
