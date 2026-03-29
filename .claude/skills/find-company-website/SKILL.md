---
name: find-company-website
description: >
  Find a company's official website/homepage given trade CSV data (company name, address,
  HS code, product description, country). Strategy 1: Gemini LLM lookup. Strategy 2: Serper
  Google Search + browser-use agent verification that visits candidate pages and compares
  content against CSV data to confirm the match.
argument-hint: "[company_name] [address] [product_desc] [country]"
---

# Find Company Website

You are an agent that discovers the official website URL for a company given trade/customs CSV data.

## When to Use This Skill

Use this skill when:
- You have a company name from a trade CSV file and need its website
- You are enriching import/export records with company web data
- You need to look up a company's homepage before extracting structured info with `/extract-company-info`

## Input

Parse arguments into a JSON object with these fields:

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `company_name` | Yes | Company name from the CSV | `SWIFT BEEF COMPANY` |
| `address` | No | Company address from CSV | `1770 PROMONTORY CIRCLE, GREELEY, CO 80634` |
| `product_desc` | No | Product description or HS code description | `CARGO IS STOWED IN A REFRIGERATED CONTAINER` |
| `country` | No | Country of origin/destination | `KR, REPUBLIC OF KOREA` |
| `hs_code` | No | HS/tariff code | `492740` |

## How to Run

### Single company lookup

```bash
python ${CLAUDE_SKILL_DIR}/scripts/find_website.py '{"company_name": "$ARGUMENTS"}'
```

If the user provides structured data, build the full JSON:

```bash
python ${CLAUDE_SKILL_DIR}/scripts/find_website.py '{"company_name": "SWIFT BEEF COMPANY", "address": "1770 PROMONTORY CIRCLE, GREELEY, CO 80634", "product_desc": "REFRIGERATED CONTAINER", "country": "KR, REPUBLIC OF KOREA"}'
```

### Batch processing from CSV

```python
import asyncio, csv, sys, os
sys.path.insert(0, "${CLAUDE_SKILL_DIR}/scripts")
from find_website import process_csv_row

async def batch():
    with open("Exports.csv") as f:
        for row in csv.DictReader(f):
            result = await process_csv_row(row, "us_export")
            print(f"{row['US_Exporter']}: {result['url']} (via {result['method']})")

asyncio.run(batch())
```

Source type mapping:
- `us_export` → `US_Exporter`, `US_Exporter_Address`, `Product_Detailed_Description`, `Country_of_Foreign_Port`, `HS_Code`
- `us_import` → `Shipper Name`, `Consignee Name` + addresses, `Product Desc`, `Country`, `HS Code`
- `co_export` → `RAZON_SOCIAL_EXPORTADOR`, `DIREC_EXPORTADOR`, `PAIS_DESTINO_FINAL`, `SUBPARTIDA`

## Strategy Design

### Strategy 1: Gemini LLM (quick knowledge lookup)

- **Requires**: `GEMINI_API_KEY` (or whatever provider is set in `GEMINI_MODEL_CONFIG`)
- **How**: Asks the LLM to identify the company's website from its training knowledge. Sends company name, address, product, and country as context.
- **Best for**: Well-known companies, large exporters/importers with clear web presence.
- **Accepts if**: URL returned + passes `validate_url()` (HTTP check, not a directory/parked domain).

### Strategy 2: Serper Search + Browser Verification

Two-phase approach that avoids Google CAPTCHAs:

**Phase 1 — Serper API search** (`SERPER_API_KEY` required):
- Searches Google via Serper.dev REST API (no browser, no CAPTCHA risk)
- Returns up to 10 organic results, each scored by:
  - Domain-name fuzzy match against company name (max +3.0)
  - Title contains company name words (max +2.0)
  - Snippet contains company name words (max +1.0)
  - Bonus for being a homepage URL (+0.5)
- Filters out directory sites (LinkedIn, Bloomberg, ImportGenius, etc.)

**Phase 2a — High confidence (score >= 4.0)**:
- If the top result scores high enough, accept it directly after basic `validate_url()` check.
- No browser needed — the domain match + title match is strong enough.

**Phase 2b — Browser verification (score < 4.0)**:
- For ambiguous results, a browser-use agent visits each candidate (up to 3).
- The agent reads the page content and compares against the CSV data:
  - Does the company name match (or is it a parent/subsidiary)?
  - Does the address or location match?
  - Are the products/services consistent with the trade goods?
- Agent returns YES/NO with reasoning.
- First verified candidate is accepted.

**Why not search Google with the browser?** Google CAPTCHAs and rate limits make direct browser search unreliable. Serper handles the search via API, and the browser only visits candidate company pages.

## Output Format — Agent-Readable

The script returns the **full results from both strategies** so you can reason about them. No hardcoded confidence thresholds — you decide what to accept.

```json
{
  "canonical_name": "SWIFT BEEF",
  "input": {"company_name": "SWIFT BEEF COMPANY", "address": "GREELEY, CO", ...},
  "gemini": {
    "url": "https://swiftbeef.com",
    "confidence": 8,
    "reason": "Well-known US beef company headquartered in Greeley, CO",
    "url_valid": true
  },
  "serper": {
    "url": "https://swiftbeef.com",
    "query": "SWIFT BEEF COMPANY GREELEY, CO official website",
    "candidates": [
      {"url": "https://swiftbeef.com", "score": 5.5, "title": "Swift Beef Company"},
      {"url": "https://jbsusa.com", "score": 2.1, "title": "JBS USA - Parent Company"}
    ]
  },
  "recommendation": {
    "url": "https://swiftbeef.com",
    "method": "serper_browser",
    "company_id": "swiftbeef.com"
  },
  "status": "found"
}
```

### How to interpret the output

**`gemini`** — LLM knowledge lookup:
- `confidence` (1-10): How sure the LLM is. 9-10 = certain, 5-6 = moderate, 1-2 = guessing.
- `reason`: Why it chose this URL or why it's unsure. Read this to understand edge cases.
- `url_valid`: Whether the URL passed HTTP liveness + parked domain checks.

**`serper`** — Google search + browser verification:
- `url`: The browser-verified URL (strongest evidence), or null if none verified.
- `candidates`: Top 5 search results with relevance scores. Useful if the verified URL is null — you might want to investigate specific candidates.

**`recommendation`** — The script's best guess. You can override this based on:
- Gemini confidence + reason vs Serper candidate scores
- Whether Gemini and Serper agree on the same URL (strong signal)
- Whether the reason reveals ambiguity (subsidiary, parent company, rebranding)

### Decision guidelines for the agent

- **Both strategies agree** on the same URL → high confidence, accept it
- **Gemini confidence >= 7** and `url_valid=true` → likely correct, accept unless reason raises concerns
- **Gemini confidence 4-6** → moderate, check if Serper candidates confirm or contradict
- **Gemini confidence <= 3** or no URL → rely on Serper results
- **Serper has verified URL** (`serper.url` is not null) → browser confirmed the match, trust it
- **No verified URL but candidates exist** → you may want to investigate the top candidates manually
- **Both strategies return null** → company likely has no website, or name is too ambiguous

## Storing Results in Typesense

```python
sys.path.insert(0, "path/to/extract-company-info/scripts")
from db import get_typesense_client, init_schema, get_or_create_company, update_company_website

client = get_typesense_client()
init_schema(client)
rec = result["recommendation"]
if rec:
    company_id = get_or_create_company(client, result["canonical_name"], rec["url"])
    update_company_website(client, company_id, rec["url"], rec["method"])
```

## Required Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `GEMINI_API_KEY` | Recommended | For Strategy 1 (Gemini LLM lookup) |
| `SERPER_API_KEY` | Recommended | For Strategy 2 (Serper Google Search) |
| `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` | Optional | For browser verification agent (Strategy 2b) |
| `GEMINI_MODEL_CONFIG` | Optional | Models.yaml config key for LLM (default: `gemini-2.5-flash`) |
| `BROWSER_MODEL_CONFIG` | Optional | Models.yaml config key for browser agent (default: `gpt-4o`) |

## Error Handling

- If Gemini returns "UNKNOWN" or invalid URL → falls through to Strategy 2
- If Serper returns no results → returns `not_found`
- If browser-use is not installed → high-confidence Serper results still work, ambiguous ones are skipped
- All errors captured in the `strategies` array for debugging
- Never raises exceptions to the caller

## Scripts Reference

| File | Purpose |
|------|---------|
| `scripts/find_website.py` | Main orchestrator — 2-strategy cascade |
| `scripts/strategy_gemini.py` | Gemini LLM website identification |
| `scripts/strategy_serper.py` | Serper search + browser-use verification |
| `scripts/cross_validate.py` | URL validation, parked domain detection |
| `scripts/normalize.py` | Company name normalization, fuzzy matching |
