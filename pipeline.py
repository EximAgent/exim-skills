#!/usr/bin/env python3
"""Full trade data pipeline.

Reads a raw xlsx trade file, finds company websites, and ingests everything into ClickHouse.

Steps:
  1. Extract unique company names from xlsx
  2. Check company_websites table → skip names already found
  3. Gemini LLM triage (confidence 0/1/2, multi-company splits)
  4. Claude agent search (Haiku, search + CF crawl to verify)
  5. Write found websites → company_websites table
  6. Ingest all trade records → tradeData table (with websites joined)

Usage:
    python pipeline.py data/Imports.xlsx --source us_import
    python pipeline.py data/01_Exportaciones_2026_Enero.xlsx --source co_export
    python pipeline.py data/test_20_us.jsonl --source us_import --skip-ingest
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import openpyxl
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

_ROOT = Path(__file__).parent
sys.path.insert(0, str(_ROOT))

from llm import create_agent_from_config
from schemas import Company, normalize_row, SOURCE_FIELD_MAP, clean_str
from tools import search_serper_batch, crawl_url
from tools.db import get_client, create_tables, lookup_websites, insert_websites, insert_trade_records


# =================== Config ===================

def _require_env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


KNOWLEDGE_CONFIG = _require_env("KNOWLEDGE_MODEL_CONFIG")
_require_env("SERPER_API_KEY")
_require_env("CLOUDFLARE_API_KEY")
_require_env("CF_ACCOUNT_ID")
AGENT_MODEL = os.environ.get("AGENT_MODEL", "haiku")
AGENT_MAX_BUDGET = float(os.environ.get("AGENT_MAX_BUDGET", "0.5"))
AGENT_MAX_TURNS = int(os.environ.get("AGENT_MAX_TURNS", "15"))
AGENT_API_BASE_URL = os.environ.get("AGENT_API_BASE_URL")
AGENT_API_KEY = os.environ.get("AGENT_API_KEY")


def _custom_agent_env() -> dict:
    if not AGENT_API_BASE_URL:
        return {}
    return {"env": {
        "ANTHROPIC_BASE_URL": AGENT_API_BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": AGENT_API_KEY or "",
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": AGENT_MODEL,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": AGENT_MODEL,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": AGENT_MODEL,
        "ANTHROPIC_SMALL_FAST_MODEL": AGENT_MODEL,
    }}


# =================== JSONL helpers ===================

def read_jsonl(path: str) -> list[Company]:
    with open(path) as f:
        return [Company(**json.loads(line)) for line in f if line.strip()]


def write_jsonl(path: str, records: list[Company]):
    with open(path, "w") as f:
        for r in records:
            f.write(r.model_dump_json(exclude_none=True) + "\n")


# =================== Step 1: Extract companies from xlsx ===================

def _parse_source(source: str) -> tuple[str, str]:
    """Parse source string into (country, direction). e.g. 'us_import' → ('us', 'import')"""
    parts = source.split("_", 1)
    return parts[0], parts[1] if len(parts) > 1 else "export"


def extract_companies_from_xlsx(
    xlsx_path: str,
    source: str,
    limit: int | None = None,
    filter_dest: str | None = None,
) -> tuple[list[Company], list[dict]]:
    """Read xlsx, extract ALL unique company names (both exporter + importer) and raw rows.

    Args:
        limit: max number of raw rows to extract (None = all). Companies are deduped across all rows.
        filter_dest: only include rows where destination country matches (e.g. 'USA')

    Returns:
        (companies, raw_rows) where companies is deduped list of ALL parties
        and raw_rows is filtered raw rows for later ingestion.
    """
    country, _ = _parse_source(source)

    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    ws = wb.active
    headers = None
    raw_rows = []
    seen_names: dict[str, Company] = {}

    field_map = SOURCE_FIELD_MAP[source]
    dest_field = next((k for k, v in field_map.items() if v == "destination_country"), None)
    exp_name_field = next((k for k, v in field_map.items() if v == "exporter_name"), None)
    exp_addr_field = next((k for k, v in field_map.items() if v == "exporter_address"), None)
    imp_name_field = next((k for k, v in field_map.items() if v == "importer_name"), None)
    imp_addr_field = next((k for k, v in field_map.items() if v == "importer_address"), None)
    hs_field = next((k for k, v in field_map.items() if v == "hs_code"), "")
    prod_field = next((k for k, v in field_map.items() if v == "product_description"), "")

    def _add_company(name: str, addr: str | None, hs: str | None, product: str | None, country_guess: str):
        if not name or name in seen_names:
            return
        seen_names[name] = Company(
            company_name=name,
            raw_name=name,  # preserve original name from xlsx
            address=addr,
            hs_code=hs,
            product_desc=product[:100] if product else None,
            country=country_guess,
        )

    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            headers = [str(h).strip() if h else "" for h in row]
            continue
        vals = list(row)
        row_dict = {headers[j]: vals[j] for j in range(min(len(headers), len(vals)))}

        if filter_dest and dest_field:
            dest = str(row_dict.get(dest_field, "") or "").upper().strip()
            if dest != filter_dest.upper():
                continue

        raw_rows.append(row_dict)

        hs = clean_str(row_dict.get(hs_field))
        product = clean_str(row_dict.get(prod_field))

        # Exporter
        if exp_name_field:
            exp_name = clean_str(row_dict.get(exp_name_field))
            exp_addr = clean_str(row_dict.get(exp_addr_field)) if exp_addr_field else None
            _add_company(exp_name, exp_addr, hs, product, country.upper())

        # Importer
        if imp_name_field:
            imp_name = clean_str(row_dict.get(imp_name_field))
            imp_addr = clean_str(row_dict.get(imp_addr_field)) if imp_addr_field else None
            imp_country = str(row_dict.get(dest_field, "") or "").upper().strip() if dest_field else ""
            _add_company(imp_name, imp_addr, hs, product, imp_country)

        if limit and len(raw_rows) >= limit:
            break

    wb.close()
    companies = list(seen_names.values())
    print(f"Extracted {len(companies)} unique companies (exporters + importers) from {len(raw_rows)} rows")
    return companies, raw_rows


# =================== Step 2: Check existing websites ===================

def filter_new_companies(companies: list[Company], source: str, db_client) -> list[Company]:
    """Check company_websites table by raw_name, return only companies that need searching."""
    raw_names = [c.raw_name or c.company_name for c in companies]
    existing = lookup_websites(db_client, source, raw_names)

    new = [c for c in companies if (c.raw_name or c.company_name) not in existing]
    print(f"Already have websites for {len(existing)}/{len(companies)} raw names")
    print(f"Need to search: {len(new)}")
    return new


# =================== Step 3: LLM Triage ===================

async def ask_llm(company: Company, semaphore: asyncio.Semaphore, pbar_cost: dict) -> dict:
    prompt = f"""Given the following company information from international trade records, identify the official website URL of this company based on your knowledge.

{company.to_llm_context()}

IMPORTANT: The company name field sometimes contains MULTIPLE companies maybe separated by "//", "/", "C/O" or other symbols. If you detect multiple distinct companies, return one entry per company. Note: "DBA" means "doing business as" — that is the SAME company with a trade name, not two companies.

Always respond with a JSON list wrapped in ```json``` — even for a single company:

```json
[
  {{"name": "COMPANY NAME", "url": "https://example.com" or null, "confidence": 0 or 1 or 2, "reason": "brief explanation"}}
]
```

Confidence levels:
- 0: I don't know this company or its website
- 1: I have a guess but I'm not sure
- 2: I'm confident this is the correct official website

Rules:
- Return the homepage URL only (e.g., https://example.com, not a subpage)
- Do not return social media profiles, directory listings, or aggregator sites
- The company may be known by a parent company name or subsidiary
- Be honest — if you're not sure, use confidence 0 or 1"""

    async with semaphore:
        try:
            agent = create_agent_from_config(KNOWLEDGE_CONFIG)
            response = await agent.async_completions([{"role": "user", "content": prompt}])
            raw_text = (response.content or "").strip()
            cost = response.token_usage.cost if response.token_usage else 0.0
            tokens = response.token_usage.total_tokens if response.token_usage else 0
        except Exception as e:
            return {"raw": None, "error": str(e), "cost": 0.0, "tokens": 0}

        pbar_cost["total"] += cost
        return {"raw": raw_text, "cost": cost, "tokens": tokens}


async def step3_llm_triage(records: list[Company], concurrency: int = 10) -> list[Company]:
    print(f"\n{'='*60}")
    print(f"Step 3: LLM triage ({len(records)} companies, {concurrency} concurrent)")
    print(f"{'='*60}")

    semaphore = asyncio.Semaphore(concurrency)
    pbar_cost = {"total": 0.0}
    results = [None] * len(records)
    pbar = tqdm(total=len(records), desc="ask_llm")

    async def _run(idx, c):
        result = await ask_llm(c, semaphore, pbar_cost)
        results[idx] = result
        pbar.set_postfix(cost=f"${pbar_cost['total']:.4f}")
        pbar.update(1)

    await asyncio.gather(*[_run(i, c) for i, c in enumerate(records)])
    pbar.close()

    # Parse results — handle multi-company splits
    new_records: list[Company] = []
    for record, result in zip(records, results):
        raw = result.get("raw", "")
        record.llm_cost = result.get("cost", 0.0)
        record.llm_tokens = result.get("tokens", 0)

        companies_list = None
        json_block = re.search(r'```json\s*(\[.*?\])\s*```', raw, re.DOTALL)
        try:
            if json_block:
                companies_list = json.loads(json_block.group(1))
            else:
                companies_list = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass

        if not isinstance(companies_list, list):
            record.llm_url = None
            record.llm_confidence = 0
            record.llm_reason = raw or "parse_error"
            new_records.append(record)
            continue

        # raw_name is always preserved (the original from xlsx).
        # If LLM returned 1 entry, keep the original company_name.
        # If LLM split into multiple, replace company_name with the split names.
        if len(companies_list) == 1:
            sub = companies_list[0]
            record.llm_url = sub.get("url")
            record.llm_confidence = sub.get("confidence", 0)
            record.llm_reason = sub.get("reason", "")
            new_records.append(record)
        else:
            for sub in companies_list:
                new_company = record.model_copy()
                # raw_name stays the same (original xlsx form)
                new_company.company_name = sub.get("name", record.company_name)
                new_company.llm_url = sub.get("url")
                new_company.llm_confidence = sub.get("confidence", 0)
                new_company.llm_reason = sub.get("reason", "")
                new_records.append(new_company)

    records = new_records
    c2 = sum(1 for r in records if r.llm_confidence == 2)
    c1 = sum(1 for r in records if r.llm_confidence == 1)
    c0 = sum(1 for r in records if (r.llm_confidence or 0) == 0)
    total_cost = sum(r.llm_cost or 0 for r in records)

    print(f"\nLLM triage: conf=2: {c2}, conf=1: {c1}, conf=0: {c0}")
    if len(records) != len(results):
        print(f"Multi-company splits: {len(records) - len(results)} new records")
    print(f"Resolved (conf=2): {c2}/{len(records)} ({c2/len(records)*100:.0f}%)")
    print(f"Need agent search: {c0+c1}/{len(records)}")
    print(f"LLM cost: ${total_cost:.4f}")

    return records


# =================== Step 4: Agent Search ===================

_RESULTS_DIR = _ROOT / "data" / "agent_results"


async def step4_agent_search(records: list[Company], concurrency: int = 10) -> list[Company]:
    from claude_agent_sdk import query as sdk_query, ClaudeAgentOptions

    # Conf=2 records use LLM URL directly
    for r in records:
        if r.llm_confidence == 2 and not r.website:
            r.website = r.llm_url

    needs_search = [r for r in records if (r.llm_confidence or 0) < 2]

    if not needs_search:
        print("\nStep 4: All resolved by LLM — skipping agent search.")
        return records

    _RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"Step 4: Agent search ({len(needs_search)} companies, {concurrency} concurrent)")
    print(f"{'='*60}")

    pbar = tqdm(total=len(needs_search), desc="agents")

    async def run_agent(company: Company, result_file: Path, sem: asyncio.Semaphore) -> str | None:
        llm_context = ""
        if company.llm_url or company.llm_reason:
            llm_context = f"""
## Previous LLM Knowledge Check
A very large LLM was asked if it knows this company's website. Here's what it said:
- URL suggested: {company.llm_url or "none"}
- Confidence: {company.llm_confidence} (0=unknown, 1=guess, 2=confident)
- Reason: {company.llm_reason or "n/a"}

This was NOT confident enough to accept automatically. Use it as a hint — it may be correct, partially correct, or completely wrong. Use this as extra signal to make a better decision or more signals to search.
"""

        prompt = f"""Find the official website for this company from international trade records.

## Company Information
{company.to_llm_context()}
{llm_context}
## Available Tools

### Tool 1: Google Search via Serper

Search Google by running this Python snippet. **You construct the search query** based on what you know about the company, be smart!

```python
from tools.search import search_serper_batch
import asyncio
results = asyncio.run(search_serper_batch([("your search query here", "{company.company_name}")]))
import json; print(json.dumps(results, indent=2))
```

Returns:
```json
[{{
  "candidates": [
    {{"url": "https://swiftbeef.com", "full_url": "https://swiftbeef.com/about", "title": "Swift Beef Company", "snippet": "Leading beef producer in Greeley, CO...", "position": 1}},....
  ],
  "query": "your search query here"
}}]
```
### Tool 2: Crawl URL

When you can't tell from search snippets alone whether a URL is the right company, crawl it to get its full content as markdown:

```python
from tools.crawl_page import crawl_url
result = crawl_url("https://candidate-url.com")
print(result["title"])
print(result["markdown"][:2000])
```

Returns: `{{"url": "...", "title": "page title", "markdown": "full page content...", "status": 200}}`
Or on error: `{{"url": "...", "error": "reason"}}`

Read the markdown — it contains the company name, products, address, navigation links. Decide if it matches the company you're looking for.

## The Search Process

**You decide the search query.** Use everything you know:
- Company name (strip legal suffixes like S.A.S., LTDA for cleaner results)
- Country and address help disambiguate
- The buyer/partner name is a strong signal — if buyer is "KANU PET LLC", the exporter likely sells pet products
- Product description and HS code narrow the industry

**Evaluating results:**
- If the domain + snippet already make it obvious (e.g. `swiftbeef.com` — "Swift Beef Company | Premium Beef Products"), accept it directly — no crawl needed. Please be very careful with this.
- If there are 2–3 plausible candidates and you can't tell from the snippet alone (similar names, holding companies, subsidiaries), crawl them to confirm.
- If results are all directories or SEO pages, reject them and retry with a different query. Try to do this at least 2-3 times before giving up with appropriate reasons.

**What to REJECT:**
- Trade directories and data aggregators — even if they mention the company by name, these are NOT the company's website
- A different company with a similar name in a different country (e.g. "MAGIC FLOWERS" Ecuador vs Colombia)
- SEO/marketplace pages that aggregate company names for traffic
- Social media profiles, government registries, business listing pages

**Retry with different queries (up to 3 total searches):**
A single search query often fails because the company name is abbreviated, the brand differs from the legal name, or the first query returns only directories. You must retry — giving up after one empty result is wrong.

**Some tips for search queries on retry:**
- Zero results → try shorter name, drop legal suffix (S.A.S., LTDA), or try in the language of the origin country if available
- Subsidiary → search parent company name instead if there is more context about this
- Try `inurl:"about"` or `inurl:"quienes-somos"` in your query to target the company's About Us page — these are more likely to be the real company website than generic search results

## Output
Write your final result to this file: {result_file}

The file must contain ONLY a JSON object:
{{"website": "https://example.com", "reason": "why this is the correct website"}}
or if not found:
{{"website": null, "reason": "why no website was found"}}
"""
        async with sem:
            try:
                async for msg in sdk_query(
                    prompt=prompt,
                    options=ClaudeAgentOptions(
                        allowed_tools=["Read", "Write", "Bash", "Glob", "Grep"],
                        permission_mode="dontAsk",
                        cwd=str(_ROOT),
                        model=AGENT_MODEL,
                        max_budget_usd=AGENT_MAX_BUDGET,
                        max_turns=AGENT_MAX_TURNS,
                        **(_custom_agent_env()),
                    )
                ):
                    pass
            except Exception as e:
                print(f"\n[agent] {company.company_name}: {e}")
                pbar.update(1)
                return None

            result_url = None
            if result_file.exists():
                try:
                    parsed = json.loads(result_file.read_text().strip())
                    result_url = parsed.get("website")
                except (json.JSONDecodeError, TypeError):
                    pass

            pbar.update(1)
            return result_url

    sem = asyncio.Semaphore(concurrency)
    agent_tasks = []
    for c in needs_search:
        h = hashlib.md5(c.company_name.encode()).hexdigest()[:12]
        result_file = _RESULTS_DIR / f"{h}.json"
        result_file.unlink(missing_ok=True)
        agent_tasks.append(run_agent(c, result_file, sem))

    results = await asyncio.gather(*agent_tasks)

    for record, website in zip(needs_search, results):
        record.website = website

    for r in records:
        if r.llm_confidence == 2 and not r.website:
            r.website = r.llm_url

    pbar.close()
    found = sum(1 for r in records if r.website)
    print(f"\nTotal websites resolved: {found}/{len(records)}")

    return records


# =================== Step 5: Write websites to ClickHouse ===================

def step5_save_websites(records: list[Company], source: str, db_client):
    print(f"\n{'='*60}")
    print(f"Step 5: Save websites to ClickHouse")
    print(f"{'='*60}")

    rows = [
        (r.raw_name or r.company_name, r.company_name, r.website)
        for r in records if r.website
    ]
    insert_websites(db_client, source, rows)


# =================== Step 6: Ingest trade data ===================

def _split_port(port_str: str | None) -> tuple[str, str]:
    """Split '58023, PUSAN' into ('58023', 'PUSAN')."""
    if not port_str:
        return "", ""
    parts = port_str.split(",", 1)
    code = parts[0].strip()
    name = parts[1].strip() if len(parts) > 1 else ""
    return code, name


# Fields stored as core columns in tradeData. Everything else from normalize_row → extra JSON.
_CORE_FIELDS = {
    "id", "source", "record_date",
    "exporter_name", "importer_name",  # raw names from normalize_row (used for raw lookup)
    "origin_country", "destination_country",
    "hs_code", "hs_code_6",
    "weight_gross_kg", "quantity", "quantity_unit",
    "transport_mode",
    "loading_port_code", "loading_port_name",
    "unloading_port_code", "unloading_port_name",
    "vessel_name", "bill_of_lading", "carrier_code",
    # raw helpers (used to derive core fields, not stored)
    "loading_port", "unloading_port", "loading_port_code_raw", "unloading_port_code_raw",
    "source_id",
}


def step6_ingest_trade_data(
    raw_rows: list[dict],
    source: str,
    website_lookup: dict[str, list[tuple[str, str]]],
    db_client,
):
    """Normalize raw xlsx rows and insert into tradeData table.

    website_lookup: {raw_name: [(name, website), ...]} from company_websites.
    Multi-company splits result in multiple entries — built into arrays.
    """
    print(f"\n{'='*60}")
    print(f"Step 6: Ingest {len(raw_rows)} trade records")
    print(f"{'='*60}")

    records = []
    for row_dict in tqdm(raw_rows, desc="normalize"):
        normalized = normalize_row(row_dict, source)

        # Resolve ports — two formats:
        #   us_import: "1401, NORFOLK, VA" in loading_port (combined) → split
        #   us_export: code in loading_port_code_raw, name in loading_port (separate)
        if normalized.get("loading_port_code_raw"):
            lp_code = str(normalized["loading_port_code_raw"]).replace(".0", "")
            lp_name = normalized.get("loading_port") or ""
        elif normalized.get("loading_port"):
            lp_code, lp_name = _split_port(normalized.get("loading_port"))
        else:
            lp_code, lp_name = "", ""

        if normalized.get("unloading_port_code_raw"):
            up_code = str(normalized["unloading_port_code_raw"]).replace(".0", "")
            up_name = normalized.get("unloading_port") or ""
        elif normalized.get("unloading_port"):
            up_code, up_name = _split_port(normalized.get("unloading_port"))
        else:
            up_code, up_name = "", ""

        # Build name/website arrays from website_lookup
        exporter_raw = normalized.get("exporter_name") or ""
        importer_raw = normalized.get("importer_name") or ""

        exp_pairs = website_lookup.get(exporter_raw, [])
        imp_pairs = website_lookup.get(importer_raw, [])

        # If no entry in lookup, fall back to raw name with no website
        exp_names = [p[0] for p in exp_pairs] if exp_pairs else ([exporter_raw] if exporter_raw else [])
        exp_websites = [p[1] for p in exp_pairs] if exp_pairs else []
        imp_names = [p[0] for p in imp_pairs] if imp_pairs else ([importer_raw] if importer_raw else [])
        imp_websites = [p[1] for p in imp_pairs] if imp_pairs else []

        # Everything not in core fields → extra JSON
        extra_fields = {
            k: v for k, v in normalized.items()
            if k not in _CORE_FIELDS and v is not None and v != ""
        }

        record = {
            "id": normalized.get("id", ""),
            "source": source,
            "record_date": normalized.get("record_date") or 0,
            "exporter_raw": exporter_raw,
            "exporter_name": exp_names,
            "exporter_website": exp_websites,
            "importer_raw": importer_raw,
            "importer_name": imp_names,
            "importer_website": imp_websites,
            "origin_country": normalized.get("origin_country") or "",
            "destination_country": normalized.get("destination_country") or "",
            "hs_code": normalized.get("hs_code") or "",
            "hs_code_6": normalized.get("hs_code_6") or "",
            "weight_gross_kg": normalized.get("weight_gross_kg"),
            "quantity": normalized.get("quantity"),
            "quantity_unit": normalized.get("quantity_unit") or "",
            "transport_mode": normalized.get("transport_mode") or "",
            "loading_port_code": str(lp_code) if lp_code else "",
            "loading_port_name": str(lp_name) if lp_name else "",
            "unloading_port_code": str(up_code) if up_code else "",
            "unloading_port_name": str(up_name) if up_name else "",
            "vessel_name": normalized.get("vessel_name") or "",
            "bill_of_lading": normalized.get("bill_of_lading") or "",
            "carrier_code": normalized.get("carrier_code") or "",
            "extra": json.dumps(extra_fields, ensure_ascii=False, default=str),
        }
        records.append(record)

    batch_size = 10000
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        insert_trade_records(db_client, batch)

    print(f"Ingested {len(records)} records")


# =================== Main ===================

async def run(args):
    # Connect to ClickHouse
    db_client = None
    if not args.skip_db:
        db_client = get_client()
        create_tables(db_client)

    # Step 1: Extract companies
    print(f"\n{'='*60}")
    print(f"Step 1: Extract companies from {args.input}")
    print(f"{'='*60}")

    raw_rows = None
    if args.input.endswith(".jsonl"):
        all_companies = read_jsonl(args.input)
        print(f"Loaded {len(all_companies)} companies from JSONL")
    else:
        all_companies, raw_rows = extract_companies_from_xlsx(
            args.input, args.source,
            limit=args.limit, filter_dest=args.filter_dest,
        )

    # Step 2: Check existing websites
    new_companies = all_companies
    if db_client:
        print(f"\n{'='*60}")
        print(f"Step 2: Check existing websites in ClickHouse")
        print(f"{'='*60}")
        new_companies = filter_new_companies(all_companies, args.source, db_client)

    if not new_companies:
        print("\nAll companies already have websites. Nothing to search.")
    else:
        # Step 3: LLM triage (only for new companies)
        new_companies = await step3_llm_triage(new_companies, concurrency=args.concurrency_llm)
        write_jsonl(args.input.replace(".xlsx", ".jsonl"), new_companies)

        # Step 4: Agent search
        new_companies = await step4_agent_search(new_companies, concurrency=args.agents)
        write_jsonl(args.input.replace(".xlsx", ".jsonl"), new_companies)

    # Step 5: Save newly found websites to ClickHouse
    if db_client and new_companies:
        step5_save_websites(new_companies, args.source, db_client)

    # Step 6: Ingest trade data — lookup websites for ALL raw names from DB
    # (Step 5 already saved newly found websites, so DB has everything)
    if db_client and raw_rows and not args.skip_ingest:
        raw_names = list({c.raw_name or c.company_name for c in all_companies})
        website_lookup = lookup_websites(db_client, args.source, raw_names)
        step6_ingest_trade_data(raw_rows, args.source, website_lookup, db_client)


    # Summary
    total = len(all_companies)
    new_found = sum(1 for r in new_companies if r.website)
    total_cost = sum(r.llm_cost or 0 for r in new_companies)
    print(f"\n{'='*60}")
    print(f"DONE: {len(all_companies)} total companies, {new_found} newly found")
    print(f"LLM cost: ${total_cost:.4f}")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="Trade data pipeline")
    parser.add_argument("input", help="xlsx trade file or JSONL company list")
    parser.add_argument("--source", required=True, help="Source identifier: us_import, co_export")
    parser.add_argument("--concurrency-llm", type=int, default=10)
    parser.add_argument("--agents", type=int, default=5, help="Max concurrent Claude agents")
    parser.add_argument("--skip-ingest", action="store_true", help="Skip Step 6 (trade data ingestion)")
    parser.add_argument("--skip-db", action="store_true", help="Skip all ClickHouse steps (testing)")
    parser.add_argument("--limit", type=int, default=None, help="Limit to first N raw trade rows")
    parser.add_argument("--filter-dest", default=None, help="Only include rows with this destination country (e.g. USA)")
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
