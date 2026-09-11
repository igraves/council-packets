#!/bin/bash
# API keys live in .env (VOYAGE_API_KEY, ANTHROPIC_API_KEY) — loaded
# automatically by pvcouncil.config via python-dotenv. See .env.example.

# Index the full corpus (~5 min, ~$0.20 in Voyage API costs)
uv run pvc index

# Search
uv run pvc search "stormwater drainage"
uv run pvc search "ordinance 2428" --keyword
uv run pvc search "housing density" --type packet --after 2023-01-01

# Ask questions (RAG with Claude)
uv run pvc ask "What has the council decided about the community center?"

# Check stats
uv run pvc stats
