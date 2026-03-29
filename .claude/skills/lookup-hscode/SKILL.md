---
name: lookup-hscode
description: >
  Look up HS (Harmonized System) codes by code number or product keyword.
  Supports simple (fast, free) and advanced (LLM + semantic, costs API calls) modes.
  Use when you need to find an HS code for a product, look up what an HS code
  means, or find related tariff classifications.
argument-hint: "[query] [--mode simple|advanced] [--level section|chapter|heading|subheading] [--limit N]"
---

# Lookup HS Code

Search ~7K HS codes (H6 revision) stored in Typesense.

## How to Run

```bash
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "$ARGUMENTS"
```

## Search Modes

| Mode | Cost | When to use |
|------|------|-------------|
| `simple` (default) | Free — no API calls | Query already uses HS terminology (e.g. "bovine meat", "swine", "iron tubes") or is an HS code |
| `advanced` | Gemini API calls | Query uses informal/common terms that don't appear in HS nomenclature (e.g. "beef", "chicken", "steel pipes") |

**Decision guide for agents:** Use `simple` first. If results look irrelevant or empty, retry with `--mode advanced`.

## Examples

### Simple mode (default) — fast, no API cost
```bash
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "bovine meat"
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "0102"
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "iron tubes"
python ${CLAUDE_SKILL_DIR}/scripts/lookup.py "swine" --level heading
```

### Advanced mode — LLM expansion + semantic search
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

# Advanced mode — LLM expansion + semantic
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

**Advanced mode:**
1. LLM expands query into HS terminology (e.g. "beef" → "beef bovine meat fresh frozen chilled boneless carcasses edible offal")
2. Semantic vector search using Gemini query embedding against pre-computed HS code embeddings
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
| `GEMINI_API_KEY` | — | Required only for `--mode advanced` |
| `GEMINI_MODEL_CONFIG` | `gemini-2.5-flash` | LLM model for query expansion (advanced mode only) |

## Setup (one-time)

Index HS codes with embeddings:
```bash
python scripts/fetch_hscodes.py
```
