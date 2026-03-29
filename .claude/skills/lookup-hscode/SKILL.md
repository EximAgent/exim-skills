---
name: lookup-hscode
description: >
  Look up HS (Harmonized System) codes by code number or product keyword.
  Three modes: simple (lexical, free), hybrid (lexical + semantic), advanced (LLM expansion + hybrid).
  Use when you need to find an HS code for a product, look up what an HS code
  means, or find related tariff classifications.
argument-hint: "[query] [--mode simple|hybrid|advanced] [--level section|chapter|heading|subheading] [--limit N]"
---

# Lookup HS Code

Search ~7K HS codes (H6 revision) stored in Typesense.

## How to Run

```bash
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "$ARGUMENTS"
```

## Search Modes

| Mode | API cost | Speed | When to use |
|------|----------|-------|-------------|
| `simple` (default) | Free | ~50ms | Query uses HS terminology ("bovine meat", "swine") or is an HS code |
| `hybrid` | Embedding API only | ~1s | Query is clear but may benefit from semantic matching ("meat animals") |
| `advanced` | LLM + embedding API | ~2s | Informal/common terms not in HS nomenclature ("beef", "chicken", "steel pipes") |

**Decision guide for agents:** Start with `simple`. If results look irrelevant or empty, try `hybrid`. Use `advanced` only for informal terms that need translation to HS nomenclature.

## Examples

### Simple mode (default) — fast, no API cost
```bash
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "bovine meat"
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "0102"
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "iron tubes"
```

### Hybrid mode — lexical + semantic search
```bash
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "meat animals" --mode hybrid
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "organic chemicals" --mode hybrid
```

### Advanced mode — LLM expansion + hybrid search
```bash
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "beef" --mode advanced
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "chicken" --mode advanced
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "steel pipes" --mode advanced
```

### Other options
```bash
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "animals" --level chapter
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "steel" --limit 20
```

## Programmatic Usage

```python
import asyncio, sys
sys.path.insert(0, "${CLAUDE_SKILL_DIR}/scripts")
from lookup import search_hscodes

# Simple mode (default) — no API calls
hits = asyncio.run(search_hscodes("bovine meat", limit=5))

# Hybrid mode — lexical + semantic (embedding API only)
hits = asyncio.run(search_hscodes("meat animals", limit=5, mode="hybrid"))

# Advanced mode — LLM expansion + hybrid
hits = asyncio.run(search_hscodes("beef", limit=5, mode="advanced"))

# Code search (always simple)
hits = asyncio.run(search_hscodes("0102", limit=5))

# Each hit: {"code", "name", "level", "parent_code", "full_path"}
for h in hits:
    print(f"{h['code']} — {h['name']} ({h['level']})")
```

## How Each Mode Works

**Simple mode:**
- Lexical search on `code`, `name`, `full_path` with Typesense typo tolerance (2 typos)
- No external API calls

**Hybrid mode:**
- Lexical search on `code`, `name`, `full_path` (same as simple)
- Semantic vector search using Gemini query embedding against pre-computed HS code embeddings
- Results merged: items found by both methods rank first

**Advanced mode:**
1. LLM expands query into HS terminology (e.g. "beef" → "beef bovine meat fresh frozen chilled boneless carcasses edible offal")
2. Hybrid search (lexical with expanded terms + semantic with original query embedding)
3. Results merged: items found by both methods rank first

## HS Code Hierarchy

| Level | Code Format | Example |
|-------|-------------|---------|
| Section | Roman numeral / letters | I, II, III |
| Chapter | 2 digits | 01, 02 |
| Heading | 4 digits | 0102, 0201 |
| Subheading | 6 digits | 010210, 020110 |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `TYPESENSE_API_KEY` | `xyz` | Typesense API key |
| `TYPESENSE_HOST` | `localhost` | Typesense server host |
| `TYPESENSE_PORT` | `8108` | Typesense server port |
| `TYPESENSE_PROTOCOL` | `http` | Protocol |
| `TYPESENSE_HSCODES_COLLECTION` | `hscodes` | Collection name |
| `GEMINI_API_KEY` | — | Required for `hybrid` and `advanced` modes |
| `GEMINI_MODEL_CONFIG` | `gemini-2.5-flash` | LLM model for query expansion (`advanced` mode only) |

## Setup (one-time)

Index HS codes with embeddings:
```bash
python scripts/fetch_hscodes.py
```
