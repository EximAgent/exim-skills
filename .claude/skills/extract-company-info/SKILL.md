---
name: extract-company-info
description: >
  Extract structured company information from a website URL and store it in Typesense
  with hybrid search (lexical + semantic via Gemini text-embedding-004). Tries static HTTP
  extraction first, falls back to Playwright for JS-rendered SPAs. Extracts overview,
  products/services, contact info, leadership, certifications, industries. Use after
  find-company-website discovers a URL.
argument-hint: "[url] [company_id]"
---

# Extract Company Info

You are an agent that extracts structured company data from a website and indexes it into Typesense for hybrid search.

## When to Use This Skill

Use this skill when:
- You have a company website URL and need to extract structured data from it
- You want to enrich a trade record with website-sourced company information
- You need to build a searchable index of company profiles from their websites
- This skill is typically used AFTER `/find-company-website` has discovered the URL

## Input

You receive a website URL and optional company_id as arguments.

| Field | Required | Description | Example |
|-------|----------|-------------|---------|
| `url` | Yes | Company website URL (must start with http:// or https://) | `https://www.swiftbeef.com` |
| `company_id` | No | Typesense document ID (domain name, e.g., `swiftbeef.com`). If omitted, data is extracted but not stored. | `swiftbeef.com` |

## How to Run

### Single URL extraction

```bash
python ${CLAUDE_SKILL_DIR}/scripts/extract_info.py "https://www.swiftbeef.com" swiftbeef.com
```

### From arguments

Extract company info from: **$ARGUMENTS**

Parse the first argument as the URL. If a second argument is provided, use it as the company_id. Run `extract_info.py` with these arguments.

### Batch processing

```python
import asyncio, sys, os
sys.path.insert(0, "${CLAUDE_SKILL_DIR}/scripts")
from extract_info import batch_extract

companies = [
    {"url": "https://swiftbeef.com", "company_id": "swiftbeef.com"},
    {"url": "https://www.delongco.com", "company_id": "delongco.com"},
]
results = asyncio.run(batch_extract(companies, concurrency=3))
for r in results:
    print(f"{r['url']}: {r['status']} via {r['extraction_method']}")
```

### Full pipeline (find website → extract → index)

```python
import asyncio, csv, sys, os
sys.path.insert(0, "path/to/find-company-website/scripts")
sys.path.insert(0, "${CLAUDE_SKILL_DIR}/scripts")

from find_website import process_csv_row
from extract_info import extract_company_info
from db import get_typesense_client, init_schema, get_or_create_company, update_company_website

async def pipeline():
    client = get_typesense_client()
    init_schema(client)

    with open("Exports.csv") as f:
        for row in csv.DictReader(f):
            # Step 1: Find website
            result = await process_csv_row(row, "us_export")
            if not result["url"]:
                print(f"SKIP: {row['US_Exporter']} — no website found")
                continue

            # Step 2: Store company + website (company_id = domain name)
            company_id = get_or_create_company(client, result["canonical_name"], result["url"])
            update_company_website(client, company_id, result["url"], result["method"])

            # Step 3: Extract website data and index with embeddings
            extraction = await extract_company_info(result["url"], company_id)
            print(f"OK: {row['US_Exporter']} → {result['url']} ({extraction['status']})")

asyncio.run(pipeline())
```

## Extraction Strategy

### Strategy 1: Static HTTP (fast, cheap — tried first)
- Fetches raw HTML with `httpx` (follows redirects, 20s timeout)
- Parses with BeautifulSoup + lxml
- **Extracts from homepage**: JSON-LD/schema.org, Open Graph meta tags, standard meta tags, visible text, contact info (regex for emails/phones)
- **Crawls up to 5 subpages**: /about, /contact, /products, /services, /team — identified from navigation links
- **Succeeds if**: HTTP 200 and page has >200 characters of text content
- **Falls through if**: Page has <200 chars of text (likely JS SPA), HTTP 403/blocking, CAPTCHA

### Strategy 2: Playwright Dynamic Crawl (fallback for SPAs)
- Launches headless Chromium via Playwright
- Navigates to URL and waits for `networkidle` (JS fully rendered)
- Same extraction pipeline runs on the rendered DOM
- Also crawls subpages discovered from navigation
- **Requires**: `playwright` package + Chromium installed (`playwright install chromium`)

## What Gets Extracted

| Field | Type | Source | Example |
|-------|------|--------|---------|
| `overview` | text | About page, meta description, JSON-LD description | "Swift Beef Company is a leading..." |
| `products_services` | string[] | Product/service page headings, list items | ["Premium Beef", "Cold Storage"] |
| `contact_info` | JSON | Email/phone regex, contact page | `{"emails": ["info@co.com"], "phones": ["+1-555-0100"]}` |
| `address_hq` | text | Contact page, JSON-LD PostalAddress | "1770 Promontory Circle, Greeley, CO 80634" |
| `leadership` | JSON[] | Team/leadership page | `[{"name": "John Doe", "title": "CEO"}]` |
| `certifications` | string[] | ISO/FDA/HACCP mentions anywhere | ["ISO 9001:2015", "USDA certified"] |
| `industries` | string[] | Meta keywords, page content | ["food processing", "agriculture"] |
| `year_founded` | text | About page ("founded in 1985") | "1985" |
| `employee_count` | text | About page ("5,000 employees") | "5,000" |
| `social_links` | JSON | JSON-LD sameAs, link elements | `{"linkedin": "...", "twitter": "..."}` |

## Database — Typesense (Hybrid Search)

### Architecture

Data is stored in **Typesense** as denormalized company documents. Each company is one document containing:
- Identity (name, website URL, discovery method)
- Extracted profile (overview, products, contact, leadership, etc.)
- Aggregated trade data (HS codes[], countries[], product descriptions[])
- Semantic embedding (768-dim Gemini text-embedding-004 vector)

### Company ID Convention

The `company_id` (Typesense document ID) is the **clean domain name**:
- `https://www.swiftbeef.com/about` → `swiftbeef.com`
- `http://www.example.co.uk` → `example.co.uk`

This avoids URL encoding issues and provides a stable, human-readable identifier.

### Environment Variables

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `TYPESENSE_API_KEY` | `xyz` | Yes | Typesense API key |
| `TYPESENSE_HOST` | `localhost` | Yes | Typesense server host |
| `TYPESENSE_PORT` | `8108` | Yes | Typesense server port |
| `TYPESENSE_PROTOCOL` | `http` | No | Protocol (http/https) |
| `TYPESENSE_COLLECTION` | `companies` | No | Collection name |
| `GEMINI_API_KEY` | — | Yes | Google Gemini API key — used for 768-dim embeddings |

### Search Examples

```python
from db import get_typesense_client, search_companies, hybrid_search

client = get_typesense_client()

# 1. Lexical search — find companies by keyword
results = search_companies(client, query="agricultural chemicals")

# 2. Lexical search with faceted filtering
results = search_companies(
    client,
    query="beef",
    filter_by="countries:=[IN, INDIA] && hs_codes:=[492740]",
    facet_by="hs_codes,countries,industries,certifications",
)

# 3. Hybrid search — lexical + semantic (requires GEMINI_API_KEY)
results = await hybrid_search(
    client,
    query="companies that export farming supplies to South America",
    facet_by="countries,hs_codes",
    vector_weight=0.3,  # 30% semantic, 70% lexical
)

# 4. Get a specific company by domain
from db import get_company
doc = get_company(client, "swiftbeef.com")

# 5. Browse all companies in a country
results = search_companies(client, query="*", filter_by="countries:=[US]")
```

### Typesense Collection Schema

| Field | Type | Facet | Indexed | Description |
|-------|------|-------|---------|-------------|
| `company_id` | string | — | Yes | Document ID = clean domain name |
| `canonical_name` | string | — | Yes | Normalized company name |
| `website_url` | string | — | Yes | Full website URL |
| `overview` | string | — | Yes | Company description |
| `products_services` | string[] | Yes | Yes | Products/services list |
| `hs_codes` | string[] | Yes | Yes | All HS codes from trade records |
| `countries` | string[] | Yes | Yes | All trade partner countries |
| `industries` | string[] | Yes | Yes | Industry tags |
| `certifications` | string[] | Yes | Yes | ISO, FDA, HACCP, etc. |
| `trade_roles` | string[] | Yes | Yes | exporter, consignee, shipper |
| `discovery_method` | string | Yes | Yes | serper, gemini, browser |
| `extraction_method` | string | Yes | Yes | static_html, playwright |
| `trade_count` | int32 | Yes | Yes | Number of trade records |
| `total_fob_usd` | float | Yes | Yes | Aggregate FOB value |
| `embedding` | float[] | — | Yes | 768-dim Gemini semantic vector |
| `contact_info` | string | — | No | JSON blob (not indexed) |
| `leadership` | string | — | No | JSON blob (not indexed) |
| `trade_records_json` | string | — | No | JSON array of raw trade records |

## Output Format

```json
{
  "url": "https://www.swiftbeef.com",
  "company_id": "swiftbeef.com",
  "extraction_method": "static_html",
  "profile": {
    "overview": "Swift Beef Company is a leading...",
    "products_services": ["Premium Beef", "Cold Storage"],
    "contact_info": {"emails": ["info@swiftbeef.com"], "phones": ["+1-970-555-0100"]},
    "certifications": ["USDA certified", "ISO 22000"],
    "industries": ["food processing", "agriculture"]
  },
  "status": "success",
  "db_stored": true
}
```

## Dependencies

```bash
pip install httpx beautifulsoup4 lxml pydantic typesense
# For Playwright fallback (JS-rendered sites):
pip install playwright && playwright install chromium
```

## Error Handling

- If static extraction returns <200 chars text, automatically falls through to Playwright
- If both strategies fail, returns `{"status": "failed"}` — no exception raised
- Typesense storage errors are captured in `db_error` field without blocking the return
- Embedding generation failures are logged but don't prevent profile storage

## Scripts Reference

| File | Purpose |
|------|---------|
| `scripts/extract_info.py` | Main orchestrator — tries static then Playwright, stores in Typesense |
| `scripts/strategy_static.py` | HTTP + BeautifulSoup extraction with subpage crawling |
| `scripts/strategy_playwright.py` | Headless Chromium extraction for JS-rendered SPAs |
| `scripts/schema.py` | Pydantic v2 models (CompanyProfile, ContactInfo, LeadershipEntry) |
| `scripts/parsers.py` | HTML parsing: JSON-LD, Open Graph, meta tags, regex contact extraction |
| `scripts/db.py` | Typesense client, collection schema, CRUD operations, hybrid search |
| `scripts/embeddings.py` | Gemini text-embedding-004 (768-dim) with disk caching |
