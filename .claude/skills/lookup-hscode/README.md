# HS Code Lookup

Search ~7K Harmonized System (H6 revision) codes by code number or product keyword, powered by Typesense.

## Setup

### 1. Prerequisites

- Python 3.11+
- Typesense server running (see [project setup.sh](../../setup.sh))
- Environment variables configured (see `.env.example` at project root)

```bash
pip install httpx typesense pydantic python-dotenv pyyaml openai
```

### 2. Fetch & index HS codes (one-time)

This fetches ~7K HS codes from [macmap.org](https://www.macmap.org/) and indexes them into Typesense with Gemini embeddings for semantic search.

```bash
source .env
python scripts/fetch_hscodes.py
```

Output:
```
[fetch] Fetching HS codes from https://www.macmap.org/api/products-by-latest-hs-rev?revCode=H6 ...
[fetch] Got 6957 records
[db] Created collection 'hscodes' (embedding dim=3072)
[embed] Generating embeddings for 6957 records ...
[embed] Generated 6957 embeddings
[db] Imported 6957 documents (0 failures)
[done] HS codes indexed with embeddings successfully
```

Requires `GEMINI_API_KEY` for embedding generation. Embeddings are cached to `~/.cache/company_embeddings/` so re-runs are fast.

## Usage

### Search modes

| Mode | API cost | Speed | Best for |
|------|----------|-------|----------|
| `simple` (default) | Free | ~50ms | Queries using HS terminology ("bovine meat", "swine", "iron tubes") or HS codes |
| `hybrid` | Embedding API only | ~1s | Query is clear but may benefit from semantic matching ("meat animals") |
| `advanced` | LLM + embedding API | ~2s | Informal/common terms ("beef", "chicken", "steel pipes") |

### Search by HS code

```bash
python .claude/skills/lookup-hscode/scripts/lookup.py "0102"
```
```
Found 6 results for '0102' (mode=simple):

  [   heading]  0102      Live bovine animals
  [subheading]  010290    Live bovine animals : Other
  [subheading]  010239    Live bovine animals : Buffalo : Other
  ...
```

### Search by keyword (simple — lexical only)

```bash
python .claude/skills/lookup-hscode/scripts/lookup.py "bovine meat"
```

### Search by keyword (hybrid — lexical + semantic)

```bash
python .claude/skills/lookup-hscode/scripts/lookup.py "meat animals" --mode hybrid
```

### Search by keyword (advanced — LLM expansion + hybrid)

```bash
python .claude/skills/lookup-hscode/scripts/lookup.py "beef" --mode advanced
```
```
[expand] 'beef' → 'beef bovine meat fresh frozen chilled boneless carcasses edible offal'

Found 10 results for 'beef' (mode=advanced):

  [   heading]  0201      Meat of bovine animals, fresh or chilled
  [subheading]  020110    Meat of bovine animals, fresh or chilled : Carcases and half-carcases
  [   heading]  0202      Meat of bovine animals, frozen
  ...
```

### Filter by hierarchy level

```bash
python .claude/skills/lookup-hscode/scripts/lookup.py "animals" --level chapter
python .claude/skills/lookup-hscode/scripts/lookup.py "01" --level subheading
```

### More results

```bash
python .claude/skills/lookup-hscode/scripts/lookup.py "steel" --limit 20
```

## How it works

### Data structure

Each HS code is stored as a Typesense document with:

| Field | Example | Description |
|-------|---------|-------------|
| `code` | `020110` | HS code |
| `name` | `Meat of bovine animals, fresh or chilled : Carcases and half-carcases` | Official description |
| `level` | `subheading` | Hierarchy level (section / chapter / heading / subheading) |
| `parent_code` | `0201` | Parent HS code |
| `full_path` | `Live animals; animal products > Meat and edible meat offal > ...` | Full breadcrumb path |
| `embedding` | `[0.012, -0.034, ...]` | 3072-dim Gemini vector for semantic search |

### HS code hierarchy

```
Section I    "Live animals; animal products"          (roman numeral)
  Chapter 01   "Live animals"                         (2 digits)
    Heading 0102  "Live bovine animals"               (4 digits)
      Subheading 010221  "Cattle : Pure-bred breeding" (6 digits)
```

### Simple mode

Typesense lexical search across `code`, `name`, and `full_path` fields with typo tolerance (up to 2 typos). No external API calls.

### Hybrid mode

1. **Lexical search** — same as simple mode
2. **Semantic vector search** — the query is embedded via Gemini and compared against pre-computed HS code embeddings
3. **Result merging** — items found by both methods rank first, then semantic-only, then lexical-only

### Advanced mode

1. **LLM query expansion** — Gemini rewrites the query into formal HS nomenclature (e.g. "beef" becomes "beef bovine meat fresh frozen chilled boneless carcasses edible offal")
2. **Lexical search** — uses the expanded query terms
3. **Semantic vector search** — uses the original query embedding
4. **Result merging** — items found by both methods rank first, then semantic-only, then lexical-only

## Environment variables

| Variable | Default | Required for |
|----------|---------|--------------|
| `TYPESENSE_API_KEY` | `xyz` | All modes |
| `TYPESENSE_HOST` | `localhost` | All modes |
| `TYPESENSE_PORT` | `8108` | All modes |
| `TYPESENSE_PROTOCOL` | `http` | All modes |
| `TYPESENSE_HSCODES_COLLECTION` | `hscodes` | All modes |
| `GEMINI_API_KEY` | — | Indexing + hybrid/advanced modes |
| `GEMINI_MODEL_CONFIG` | `gemini-2.5-flash` | Advanced mode (query expansion) |
| `EMBEDDING_MODEL_CONFIG` | `gemini-embedding-001` | Indexing + hybrid/advanced modes |

## Files

```
scripts/
  fetch_hscodes.py          # One-time: fetch from macmap.org + index into Typesense

.claude/skills/lookup-hscode/
  SKILL.md                  # Claude Code skill definition (agent instructions)
  README.md                 # This file (human documentation)
  scripts/
    lookup.py               # Search script (simple, hybrid, advanced modes)
```
