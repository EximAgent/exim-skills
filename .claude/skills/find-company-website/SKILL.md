---
name: find-company-website
description: >
  Find a company's official website/homepage given trade CSV data (company name, address,
  HS code, product description, country). Step 1: ask an LLM if it knows the company.
  Step 2: search Google via Serper, read the raw results, and use agent-browser to
  visit and verify candidates yourself. You can retry search with different queries up to 3 times.
argument-hint: "[company_name] [address] [product_desc] [country]"
---

# Find Company Website

Given trade/customs CSV data about a company, find its official website URL.

## Why This Pipeline Exists

Trade CSV data (US imports/exports) contains company names, addresses, HS codes, and product descriptions — but no website URLs. We need to enrich these records with the company's actual website so we can later extract structured company intelligence from it (via `/extract-company-info`).

The challenge: trade data has messy company names ("SAMSNUG ELEC CO LTD"), abbreviations, and parent/subsidiary relationships. A simple Google search isn't enough — you need to verify that a URL actually belongs to the right company.

## The Pipeline — Step by Step

### Step 1: Ask a knowledgeable LLM

A foundation model may already know the company and its website from training data. This is fast, cheap, and works well for well-known companies.

```bash
python ${CLAUDE_SKILL_DIR}/scripts/ask_llm.py '{"company_name": "SWIFT BEEF COMPANY", "address": "GREELEY, CO"}'
```

This returns the LLM's raw response. Read its reasoning — if it's convincing and specific, you may be done. If it's vague, hedging, or doesn't know, move to Step 2.

### Step 2: Search Google + verify with agent-browser

When the LLM doesn't know or you don't trust its answer, search Google. **You construct the search query** based on what you know about the company.

```bash
python ${CLAUDE_SKILL_DIR}/scripts/search_google.py "Swift Beef Company Greeley Colorado official website" "SWIFT BEEF COMPANY"
```

This returns raw Google results — titles, snippets, URLs. Read them.

**If the domain + snippet already make it obvious** (e.g. `swiftbeef.com` — "Swift Beef Company | Premium Beef Products"), accept it directly — no browser needed.

**If there are 2–3 plausible candidates** and you can't tell from the snippet alone (similar names, holding companies, subsidiaries), open them with agent-browser to confirm:

```bash
agent-browser open "https://candidate-url.com"
agent-browser snapshot -c
```

Read the page. Does the content, company name, and products match? If yes, done. If you need to dig deeper:

Core agent-browser workflow:
1. `agent-browser open <url>` — Navigate to page (starts daemon on first call, reuses it after)
2. `agent-browser snapshot -c` — Compact accessibility tree to read page content
3. `agent-browser snapshot -i` — Interactive elements with refs (@e1, @e2) if you need to click
4. `agent-browser click @e1` / `fill @e2 "text"` — Interact using refs

**For batch verification — use named sessions, not open/close per company:**

```bash
# Open multiple candidates in parallel (same Chromium daemon, isolated contexts)
agent-browser --session s1 open https://candidate1.com &
agent-browser --session s2 open https://candidate2.com &
agent-browser --session s3 open https://candidate3.com &
wait

# Snapshot each independently
agent-browser --session s1 snapshot -c
agent-browser --session s2 snapshot -c

# Close all sessions at the end
agent-browser close --all
```

Never `close` between companies — it kills the Chromium daemon and wastes the startup cost on the next call. Keep the daemon alive, use `--session <name>` for isolation, and `close --all` only when fully done.

### Step 3: Retry with different queries — up to 3 rounds, batched

A single search query often fails because the company name is abbreviated, the brand differs from the legal name, or the first query returns only directories. **You must retry** — giving up after one empty result is wrong.

**In batch mode, retries work in rounds:**

```python
# Round 1: one query per uncertain company (all N of them)
round1 = [
    ("PULPAFRUIT Colombia fruit export", "PULPAFRUIT S A S"),
    ("CHESAPEAKE SEAFOOD GROUP Colombia", "CHESAPEAKE SEAFOOD GROUP S.A.S."),
    ("C.I. FLORCO flores Colombia exportacion", "C.I. FLORCO S.A."),
    # ... all N uncertain companies
]
r1_results = asyncio.run(search_serper_batch(round1))

# Read r1_results — find the subset with 0 candidates or only directory hits
# Build NEW queries only for those failures, fire round 2
round2 = [
    ("pulpafruit.com", "PULPAFRUIT S A S"),              # rephrased
    ("florco ciflowersofcolombia Colombia", "C.I. FLORCO S.A."),  # rephrased
    # CHESAPEAKE found in round 1 — not included here
]
r2_results = asyncio.run(search_serper_batch(round2))

# Still missing after round 2? One final round 3 for those stragglers only
```

When rephrasing a failed query, think about why it failed:
- All directory results → add product type or drop generic words like "official website"
- Zero results → try shorter name, drop legal suffix (S.A.S., LTDA), or try in Spanish
- Subsidiary → search parent company name instead
- Acronym → try the full expanded name

After 3 rounds with no good candidate, mark as `not_found` — the company likely has no web presence.

## Input

| Field | Required | Example |
|-------|----------|---------|
| `company_name` | Yes | `SWIFT BEEF COMPANY` |
| `address` | No | `1770 PROMONTORY CIRCLE, GREELEY, CO 80634` |
| `product_desc` | No | `CARGO IS STOWED IN A REFRIGERATED CONTAINER` |
| `country` | No | `KR, REPUBLIC OF KOREA` |
| `hs_code` | No | `492740` |

## Batch Processing

Both `ask_llm.py` and `search_google.py` support native batch mode with async concurrency. Pass a list and get a list back — progress is shown via tqdm.

**CLI** — always pass a JSON list, returns a list in the same order:

```bash
python scripts/ask_llm.py '[{"company_name": "SWIFT BEEF COMPANY"}, {"company_name": "TYSON FOODS INC"}]'
```

```python
import asyncio, sys
sys.path.insert(0, "${CLAUDE_SKILL_DIR}/scripts")
from ask_llm import ask_llm_batch
from search_google import search_serper_batch

# Step 1: ask LLM about many companies at once (default 10 concurrent)
companies = [
    {"company_name": "SWIFT BEEF COMPANY", "address": "GREELEY, CO"},
    {"company_name": "TYSON FOODS INC", "country": "US"},
]
results = asyncio.run(ask_llm_batch(companies))
# results[i]["raw"] is the LLM's response for companies[i]

# Step 2: search Google for many queries at once (default 5 concurrent)
queries = [
    ("Swift Beef Company Greeley CO official site", "SWIFT BEEF COMPANY"),
    ("Tyson Foods Inc headquarters website", "TYSON FOODS INC"),
]
search_results = asyncio.run(search_serper_batch(queries))
# search_results[i]["candidates"] is the list of Google hits for queries[i]
```

Concurrency defaults (override via env):
- `ASK_LLM_CONCURRENCY` — parallel LLM calls (default: 10)
- `SERPER_CONCURRENCY` — parallel Serper API calls (default: 5, keep low to avoid rate limits)

Serper has no native batch endpoint — parallel async calls with a semaphore is the right approach.

## Output Formats

### ask_llm.py (Step 1)

```json
{
  "input": {"company_name": "SWIFT BEEF COMPANY", "address": "GREELEY, CO"},
  "raw": "{\"url\": \"https://swiftbeef.com\", \"confidence\": 8, \"reason\": \"Well-known US beef company headquartered in Greeley, CO\"}"
}
```

The `raw` field is the LLM's response as-is. You parse it, read the reasoning, and decide whether to trust it.

### search_google.py (Step 2)

```json
{
  "query": "Swift Beef Company Greeley CO official website",
  "candidates": [
    {"url": "https://swiftbeef.com", "full_url": "https://swiftbeef.com/", "title": "Swift Beef Company - Premium Beef Products", "snippet": "Swift Beef Company is a leading producer...", "position": 1},
    {"url": "https://jbsusa.com", "full_url": "https://jbsusa.com/our-brands/swift/", "title": "Swift - JBS USA", "snippet": "Swift is a brand of JBS USA...", "position": 2}
  ]
}
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Recommended | For Step 1 (LLM knowledge lookup) |
| `SERPER_API_KEY` | Recommended | For Step 2 (Google search) |
| `KNOWLEDGE_MODEL_CONFIG` | Optional | Models.yaml config key for LLM |

## Required Tools

| Tool | Install |
|------|---------|
| `agent-browser` | `npm install -g agent-browser && agent-browser install` |

## Scripts

| File | Purpose |
|------|---------|
| `scripts/ask_llm.py` | Step 1 — ask a foundation model if it knows the company's website |
| `scripts/search_google.py` | Step 2 — search Google via Serper (agent constructs query, callable multiple times) |
| `scripts/cross_validate.py` | Directory domain filtering (used internally by search_google.py) |
