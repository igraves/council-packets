"""Paths, env vars, constants."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DOCS_DIR = PROJECT_ROOT / "pv_council_docs"
METADATA_PATH = DOCS_DIR / "metadata.json"
CHROMA_DIR = PROJECT_ROOT / "chroma_db"
COLLECTION_NAME = "council_docs"

# Load .env from project root (won't override existing env vars)
load_dotenv(PROJECT_ROOT / ".env")

# Chunking
CHUNK_SIZE_TOKENS = 500
CHUNK_OVERLAP_TOKENS = 50
CHARS_PER_TOKEN = 4  # rough estimate for English text

# API keys (from environment or .env)
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Models
EMBEDDING_MODEL = "voyage-3-lite"
CLAUDE_MODEL = "claude-sonnet-5"

# Search defaults
DEFAULT_N_RESULTS = 10
RAG_N_CHUNKS = 15
