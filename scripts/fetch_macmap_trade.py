"""One-time script: crawl macmap.org trade data and index into Typesense.

Flow:
1. For each reporter + HS subheading code → fetch NTLC (national tariff line)
   product codes via /api/v2/ntlc-products
2. For each NTLC code + partner → fetch tariffs, trade remedies, NTM measures,
   and custom duties via /api/results/* endpoints
3. Index results into Typesense

Usage:
    # Crawl default shortlist of reporter countries
    python scripts/fetch_macmap_trade.py

    # Crawl specific reporters
    python scripts/fetch_macmap_trade.py --reporters 842,704,356

    # Crawl specific reporters with specific partners
    python scripts/fetch_macmap_trade.py --reporters 842 --partners 704,156,392

    # Limit total combos (for testing)
    python scripts/fetch_macmap_trade.py --reporters 842 --limit 10

    # Adjust concurrency
    python scripts/fetch_macmap_trade.py --concurrency 3

    # Resume from checkpoint
    python scripts/fetch_macmap_trade.py --resume
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
import httpx

load_dotenv()
import typesense
from typesense.exceptions import ObjectNotFound

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COLLECTION_NAME = os.environ.get("TYPESENSE_MACMAP_TRADE_COLLECTION", "macmap_trade")

CACHE_DIR = Path.home() / ".cache" / "macmap_crawl"
CHECKPOINT_FILE = CACHE_DIR / "checkpoint.json"

BASE_URL = "https://www.macmap.org"

API_ENDPOINTS = {
    "taxes": "/api/results/taxes",
    "traderemedy": "/api/results/traderemedy",
    "ntm_measures": "/api/results/ntm-measures",
    "customduties": "/api/results/customduties",
}

DEFAULT_REPORTERS = [
    "842",  # United States
    "704",  # Vietnam
    "356",  # India
    "156",  # China
    "392",  # Japan
    "410",  # South Korea
    "276",  # Germany
    "826",  # United Kingdom
    "076",  # Brazil
    "484",  # Mexico
    "360",  # Indonesia
    "764",  # Thailand
    "036",  # Australia
    "124",  # Canada
    "250",  # France
]

HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/json; charset=utf-8",
    "referer": "https://www.macmap.org/",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "x-requested-with": "XMLHttpRequest",
}

TRADE_SCHEMA = {
    "name": COLLECTION_NAME,
    "fields": [
        {"name": "reporter_code", "type": "string", "facet": True},
        {"name": "reporter_name", "type": "string"},
        {"name": "partner_code", "type": "string", "facet": True},
        {"name": "partner_name", "type": "string"},
        {"name": "product_code", "type": "string", "facet": True},
        {"name": "hs_code", "type": "string", "facet": True, "optional": True},
        {"name": "product_description", "type": "string", "optional": True},
        {"name": "applied_tariff", "type": "string", "facet": True, "optional": True},
        {"name": "ave_tariff", "type": "string", "optional": True},
        {"name": "tariff_regime", "type": "string", "facet": True, "optional": True},
        {"name": "trade_remedy_types", "type": "string[]", "facet": True, "optional": True},
        {"name": "ntm_measure_count", "type": "int32", "optional": True},
        {"name": "ntm_measure_titles", "type": "string[]", "optional": True},
        {"name": "has_taxes", "type": "bool", "facet": True, "optional": True},
        {"name": "summary_text", "type": "string", "optional": True},
        {"name": "custom_duties_json", "type": "string", "optional": True, "index": False},
        {"name": "taxes_json", "type": "string", "optional": True, "index": False},
        {"name": "trade_remedies_json", "type": "string", "optional": True, "index": False},
        {"name": "ntm_measures_json", "type": "string", "optional": True, "index": False},
        {"name": "year", "type": "string", "facet": True, "optional": True},
        {"name": "crawled_at", "type": "int64"},
    ],
    "token_separators": ["-", "."],
}


# ---------------------------------------------------------------------------
# Typesense helpers
# ---------------------------------------------------------------------------

def get_typesense_client() -> typesense.Client:
    return typesense.Client({
        "api_key": os.environ.get("TYPESENSE_API_KEY", "xyz"),
        "nodes": [{
            "host": os.environ.get("TYPESENSE_HOST", "localhost"),
            "port": os.environ.get("TYPESENSE_PORT", "8108"),
            "protocol": os.environ.get("TYPESENSE_PROTOCOL", "http"),
        }],
        "connection_timeout_seconds": 10,
    })


def ensure_collection(client: typesense.Client) -> None:
    """Create collection if it doesn't exist."""
    try:
        client.collections[COLLECTION_NAME].retrieve()
        print(f"[db] Collection '{COLLECTION_NAME}' already exists")
    except ObjectNotFound:
        client.collections.create(TRADE_SCHEMA)
        print(f"[db] Created collection '{COLLECTION_NAME}'")


# ---------------------------------------------------------------------------
# Cookie management via Playwright
# ---------------------------------------------------------------------------

async def get_fresh_cookies() -> dict[str, str]:
    """Get cookies for macmap.org API requests.

    Tries minimal cookies first (Culture=en works for most endpoints).
    Falls back to Playwright browser session if MACMAP_USE_PLAYWRIGHT=1 is set.
    Also supports MACMAP_COOKIES env var for manual cookie injection.
    """
    # Check for manual cookie override
    manual_cookies = os.environ.get("MACMAP_COOKIES")
    if manual_cookies:
        cookies = {}
        for part in manual_cookies.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()
        cookies["Culture"] = "en"
        print(f"[cookie] Using manual cookies from MACMAP_COOKIES env var")
        return cookies

    # Try Playwright if requested
    if os.environ.get("MACMAP_USE_PLAYWRIGHT") == "1":
        print("[cookie] Launching browser to get fresh session cookies ...")
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                )
                page = await context.new_page()
                await page.goto(
                    f"{BASE_URL}/en//query/results?reporter=842&partner=004&product=262019&level=6",
                    wait_until="networkidle",
                    timeout=30000,
                )
                await asyncio.sleep(2)
                cookies_list = await context.cookies()
                await browser.close()

            cookies = {}
            for c in cookies_list:
                cookies[c["name"]] = c["value"]
            cookies["Culture"] = "en"
            print(f"[cookie] Got {len(cookies)} cookies via Playwright")
            return cookies
        except Exception as e:
            print(f"[cookie] Playwright failed: {e}")
            print("[cookie] Falling back to minimal cookies")

    # Default: minimal cookies (works for macmap.org APIs)
    print("[cookie] Using minimal cookies (Culture=en)")
    return {"Culture": "en"}


# ---------------------------------------------------------------------------
# Checkpoint management
# ---------------------------------------------------------------------------

def load_checkpoint() -> dict:
    """Load crawl checkpoint or return empty state."""
    if CHECKPOINT_FILE.exists():
        with open(CHECKPOINT_FILE) as f:
            return json.load(f)
    return {"completed": {}, "stats": {"total": 0, "indexed": 0, "empty": 0, "errors": 0}}


def save_checkpoint(checkpoint: dict) -> None:
    """Save crawl checkpoint."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump(checkpoint, f)


def is_completed(checkpoint: dict, reporter: str, partner: str, product: str) -> bool:
    """Check if a combo has been crawled."""
    key = f"{reporter}_{partner}_{product}"
    return key in checkpoint["completed"]


def mark_completed(checkpoint: dict, reporter: str, partner: str, product: str) -> None:
    """Mark a combo as crawled."""
    key = f"{reporter}_{partner}_{product}"
    checkpoint["completed"][key] = True


# ---------------------------------------------------------------------------
# Data fetching
# ---------------------------------------------------------------------------

async def fetch_endpoint(
    client: httpx.AsyncClient,
    endpoint: str,
    reporter: str,
    partner: str,
    product: str,
    cookies: dict[str, str],
) -> tuple[str, dict | list | None, bool]:
    """Fetch one API endpoint. Returns (endpoint_name, data, needs_cookie_refresh)."""
    url = f"{BASE_URL}{API_ENDPOINTS[endpoint]}"
    params = {"reporter": reporter, "partner": partner, "product": product}

    try:
        resp = await client.get(url, params=params, headers=HEADERS, cookies=cookies, timeout=30)
        if resp.status_code == 403:
            return endpoint, None, True  # Need cookie refresh
        resp.raise_for_status()
        data = resp.json()
        return endpoint, data, False
    except Exception as e:
        print(f"  [error] {endpoint}: {e}")
        return endpoint, None, False


async def fetch_all_endpoints(
    client: httpx.AsyncClient,
    reporter: str,
    partner: str,
    product: str,
    cookies: dict[str, str],
) -> tuple[dict[str, any], bool]:
    """Fetch all 4 endpoints for a combo. Returns (results_dict, needs_cookie_refresh)."""
    tasks = [
        fetch_endpoint(client, ep, reporter, partner, product, cookies)
        for ep in API_ENDPOINTS
    ]
    results_list = await asyncio.gather(*tasks)

    results = {}
    needs_refresh = False
    for ep_name, data, refresh in results_list:
        results[ep_name] = data
        if refresh:
            needs_refresh = True

    return results, needs_refresh


async def fetch_ntlc_products(
    client: httpx.AsyncClient,
    reporter: str,
    hs_code: str,
    cookies: dict[str, str],
) -> tuple[list[str], bool]:
    """Fetch national tariff line codes for a reporter + HS subheading.

    Returns (list_of_ntlc_codes, needs_cookie_refresh).
    """
    url = f"{BASE_URL}/api/v2/ntlc-products"
    params = {"countryCode": reporter, "level": "8", "code": hs_code}

    try:
        resp = await client.get(url, params=params, headers=HEADERS, cookies=cookies, timeout=30)
        if resp.status_code == 403:
            return [], True
        resp.raise_for_status()
        data = resp.json()
        # Extract product codes from response
        if isinstance(data, list):
            codes = [item.get("code") or item.get("Code") or item.get("ProductCode", "") for item in data]
            return [c for c in codes if c], False
        return [], False
    except Exception as e:
        print(f"  [error] ntlc-products {reporter}/{hs_code}: {e}")
        return [], False


# ---------------------------------------------------------------------------
# Data extraction helpers
# ---------------------------------------------------------------------------

def extract_document(
    reporter: str,
    reporter_name: str,
    partner: str,
    partner_name: str,
    product: str,
    results: dict,
    hs_code: str = "",
) -> dict | None:
    """Extract a Typesense document from API results. Returns None if all empty."""

    duties_data = results.get("customduties")
    taxes_data = results.get("taxes")
    remedies_data = results.get("traderemedy")
    ntm_data = results.get("ntm_measures")

    # Check if there's any actual data
    has_duties = bool(duties_data and duties_data.get("CustomDuty"))
    has_taxes = bool(taxes_data and taxes_data.get("TaxDataViewModels"))
    has_remedies = bool(remedies_data and remedies_data.get("TradeRemedyData"))
    has_ntm = bool(ntm_data and isinstance(ntm_data, list) and len(ntm_data) > 0)

    if not any([has_duties, has_taxes, has_remedies, has_ntm]):
        return None

    doc = {
        "id": f"{reporter}_{partner}_{product}",
        "reporter_code": reporter,
        "reporter_name": reporter_name,
        "partner_code": partner,
        "partner_name": partner_name,
        "product_code": product,
        "crawled_at": int(time.time()),
    }
    if hs_code:
        doc["hs_code"] = hs_code

    summary_parts = []

    # Custom duties
    if has_duties:
        duty_list = duties_data["CustomDuty"]
        first = duty_list[0]
        doc["product_description"] = first.get("NTLCDescription", "")
        doc["applied_tariff"] = first.get("TariffReported", "")
        doc["ave_tariff"] = first.get("TariffAve", "")
        doc["tariff_regime"] = first.get("TariffRegime", "")
        doc["year"] = first.get("Year") or duties_data.get("Year", "")
        doc["custom_duties_json"] = json.dumps(duties_data)
        summary_parts.append(
            f"Custom duty: {doc['applied_tariff']} ({doc['tariff_regime']}) for {doc['product_description']}"
        )

    # Taxes
    doc["has_taxes"] = has_taxes
    if has_taxes:
        doc["taxes_json"] = json.dumps(taxes_data)
        summary_parts.append("Has applicable taxes")

    # Trade remedies
    if has_remedies:
        remedy_types = list({r.get("RemedyType", "") for r in remedies_data["TradeRemedyData"] if r.get("RemedyType")})
        doc["trade_remedy_types"] = remedy_types
        doc["trade_remedies_json"] = json.dumps(remedies_data)
        summary_parts.append(f"Trade remedies: {', '.join(remedy_types)}")

    # NTM measures
    if has_ntm:
        all_titles = []
        total_count = 0
        for section in ntm_data:
            if section.get("Measures"):
                for m in section["Measures"]:
                    title = m.get("MeasureTitle", "")
                    if title:
                        all_titles.append(title)
                    total_count += int(m.get("MeasureCount", 0))
        doc["ntm_measure_count"] = total_count
        doc["ntm_measure_titles"] = list(set(all_titles))
        doc["ntm_measures_json"] = json.dumps(ntm_data)
        summary_parts.append(f"NTM measures: {total_count} ({', '.join(all_titles[:5])})")

    doc["summary_text"] = f"{reporter_name} -> {partner_name}: {product}. " + ". ".join(summary_parts)

    return doc


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def load_countries() -> dict[str, str]:
    """Load country code -> name mapping from Typesense."""
    client = get_typesense_client()
    try:
        # Fetch all countries via search
        countries = {}
        page = 1
        while True:
            result = client.collections["macmap_countries"].documents.search({
                "q": "*",
                "per_page": 250,
                "page": page,
            })
            for hit in result["hits"]:
                doc = hit["document"]
                countries[doc["code"]] = doc["name"]
            if len(result["hits"]) < 250:
                break
            page += 1
        return countries
    except Exception as e:
        print(f"[warn] Could not load countries from Typesense: {e}")
        print("[warn] Run `python scripts/fetch_macmap_countries.py` first")
        return {}


def load_hs_products() -> list[str]:
    """Load HS product codes (subheading level only) from Typesense."""
    client = get_typesense_client()
    products = []
    page = 1
    while True:
        result = client.collections["hscodes"].documents.search({
            "q": "*",
            "filter_by": "level:=subheading",
            "per_page": 250,
            "page": page,
        })
        for hit in result["hits"]:
            products.append(hit["document"]["code"])
        if len(result["hits"]) < 250:
            break
        page += 1
    print(f"[data] Loaded {len(products)} HS subheading codes")
    return products


# ---------------------------------------------------------------------------
# Main crawl loop
# ---------------------------------------------------------------------------

async def crawl(
    reporters: list[str],
    partners: list[str] | None = None,
    limit: int | None = None,
    concurrency: int = 5,
    resume: bool = False,
    batch_delay: float = 0.2,
) -> None:
    """Main crawl function.

    Flow:
    1. For each reporter + HS subheading code → fetch NTLC product codes
    2. For each NTLC code + partner → fetch 4 results endpoints
    3. Index into Typesense
    """

    # Load reference data
    countries = load_countries()
    if not countries:
        print("[error] No countries loaded. Run fetch_macmap_countries.py first.")
        sys.exit(1)

    hs_codes = load_hs_products()
    if not hs_codes:
        print("[error] No HS codes loaded. Run fetch_hscodes.py first.")
        sys.exit(1)

    # Determine partner list (default: same as DEFAULT_REPORTERS)
    partner_list = partners if partners else list(DEFAULT_REPORTERS)

    # Validate reporters & partners
    for r in reporters:
        if r not in countries:
            print(f"[warn] Reporter code {r} not found in countries list")
    for p in partner_list:
        if p not in countries:
            print(f"[warn] Partner code {p} not found in countries list")

    print(f"[config] {len(reporters)} reporters × {len(hs_codes)} HS codes × {len(partner_list)} partners")

    # Load or reset checkpoint
    if resume:
        checkpoint = load_checkpoint()
        print(f"[resume] Loaded checkpoint: {checkpoint['stats']}")
    else:
        checkpoint = {"completed": {}, "stats": {"total": 0, "indexed": 0, "empty": 0, "errors": 0}}

    # Setup Typesense
    ts_client = get_typesense_client()
    ensure_collection(ts_client)

    # Get initial cookies
    cookies = await get_fresh_cookies()

    # NTLC code cache: (reporter, hs_code) -> list of NTLC codes
    ntlc_cache: dict[str, list[str]] = {}
    NTLC_CACHE_FILE = CACHE_DIR / "ntlc_cache.json"
    if resume and NTLC_CACHE_FILE.exists():
        with open(NTLC_CACHE_FILE) as f:
            ntlc_cache = json.load(f)
        print(f"[resume] Loaded {len(ntlc_cache)} cached NTLC lookups")

    # Crawl with concurrency control
    semaphore = asyncio.Semaphore(concurrency)
    batch_docs = []
    batch_count = 0
    combo_count = 0
    cookie_lock = asyncio.Lock()

    async with httpx.AsyncClient(follow_redirects=True) as http_client:

        async def get_ntlc_codes(reporter: str, hs_code: str) -> list[str]:
            """Get NTLC codes, using cache when available."""
            nonlocal cookies
            cache_key = f"{reporter}_{hs_code}"
            if cache_key in ntlc_cache:
                return ntlc_cache[cache_key]

            async with semaphore:
                codes, needs_refresh = await fetch_ntlc_products(
                    http_client, reporter, hs_code, cookies
                )
                if needs_refresh:
                    async with cookie_lock:
                        print("[cookie] Got 403 on NTLC lookup, refreshing cookies ...")
                        cookies = await get_fresh_cookies()
                        codes, _ = await fetch_ntlc_products(
                            http_client, reporter, hs_code, cookies
                        )
                ntlc_cache[cache_key] = codes
                await asyncio.sleep(batch_delay)
                return codes

        async def process_combo(
            reporter: str, partner: str, ntlc_code: str, hs_code: str,
        ) -> None:
            nonlocal cookies, batch_count, combo_count

            if is_completed(checkpoint, reporter, partner, ntlc_code):
                return

            async with semaphore:
                results, needs_refresh = await fetch_all_endpoints(
                    http_client, reporter, partner, ntlc_code, cookies
                )

                # Refresh cookies if needed
                if needs_refresh:
                    async with cookie_lock:
                        print("[cookie] Got 403, refreshing cookies ...")
                        cookies = await get_fresh_cookies()
                        results, still_needs = await fetch_all_endpoints(
                            http_client, reporter, partner, ntlc_code, cookies
                        )
                        if still_needs:
                            print(f"  [error] Still 403 after refresh: {reporter}/{partner}/{ntlc_code}")
                            checkpoint["stats"]["errors"] += 1
                            return

                reporter_name = countries.get(reporter, reporter)
                partner_name = countries.get(partner, partner)

                doc = extract_document(
                    reporter, reporter_name, partner, partner_name,
                    ntlc_code, results, hs_code=hs_code,
                )

                checkpoint["stats"]["total"] += 1
                if doc:
                    batch_docs.append(doc)
                    checkpoint["stats"]["indexed"] += 1
                else:
                    checkpoint["stats"]["empty"] += 1

                mark_completed(checkpoint, reporter, partner, ntlc_code)
                batch_count += 1
                combo_count += 1

                # Batch upsert and checkpoint every 100 combos
                if batch_count % 100 == 0:
                    if batch_docs:
                        try:
                            ts_client.collections[COLLECTION_NAME].documents.import_(
                                batch_docs, {"action": "upsert"}
                            )
                        except Exception as e:
                            print(f"  [error] Batch upsert failed: {e}")
                        batch_docs.clear()

                    save_checkpoint(checkpoint)
                    # Also save NTLC cache
                    CACHE_DIR.mkdir(parents=True, exist_ok=True)
                    with open(NTLC_CACHE_FILE, "w") as f:
                        json.dump(ntlc_cache, f)

                    stats = checkpoint["stats"]
                    print(
                        f"[progress] {stats['total']} processed | "
                        f"indexed={stats['indexed']} empty={stats['empty']} errors={stats['errors']}"
                    )

                await asyncio.sleep(batch_delay)

        # Main crawl loop: reporter → HS code → NTLC lookup → partner × NTLC combos
        stopped = False
        for reporter in reporters:
            if stopped:
                break
            print(f"\n[crawl] Reporter: {countries.get(reporter, reporter)} ({reporter})")

            for hs_code in hs_codes:
                if stopped:
                    break

                # Step 1: Get NTLC codes for this reporter + HS code
                ntlc_codes = await get_ntlc_codes(reporter, hs_code)
                if not ntlc_codes:
                    continue

                # Step 2: For each NTLC code × partner, fetch results
                tasks = []
                for ntlc_code in ntlc_codes:
                    for partner in partner_list:
                        if partner == reporter:
                            continue
                        if limit and combo_count >= limit:
                            stopped = True
                            break
                        tasks.append(process_combo(reporter, partner, ntlc_code, hs_code))
                    if stopped:
                        break

                if tasks:
                    await asyncio.gather(*tasks)

    # Final flush
    if batch_docs:
        try:
            ts_client.collections[COLLECTION_NAME].documents.import_(
                batch_docs, {"action": "upsert"}
            )
        except Exception as e:
            print(f"[error] Final batch upsert failed: {e}")

    save_checkpoint(checkpoint)
    # Save final NTLC cache
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(NTLC_CACHE_FILE, "w") as f:
        json.dump(ntlc_cache, f)

    stats = checkpoint["stats"]
    print(f"\n[done] Crawl complete: indexed={stats['indexed']} empty={stats['empty']} errors={stats['errors']}")
    print(f"[done] NTLC cache: {len(ntlc_cache)} reporter+HS lookups cached")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Crawl macmap.org trade data into Typesense")
    parser.add_argument(
        "--reporters",
        type=str,
        default=None,
        help="Comma-separated reporter country codes (default: shortlist of 15 key countries)",
    )
    parser.add_argument(
        "--partners",
        type=str,
        default=None,
        help="Comma-separated partner country codes (default: same as reporters list)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max combos to crawl (for testing)")
    parser.add_argument("--concurrency", type=int, default=5, help="Max concurrent requests (default: 5)")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--delay", type=float, default=0.2, help="Delay between batches in seconds (default: 0.2)")

    args = parser.parse_args()

    reporters = args.reporters.split(",") if args.reporters else DEFAULT_REPORTERS
    partners = args.partners.split(",") if args.partners else None

    print(f"[config] Reporters: {reporters}")
    print(f"[config] Partners: {partners or 'same as reporters'}")
    print(f"[config] Concurrency: {args.concurrency}, Delay: {args.delay}s")
    if args.limit:
        print(f"[config] Limit: {args.limit}")

    asyncio.run(crawl(
        reporters=reporters,
        partners=partners,
        limit=args.limit,
        concurrency=args.concurrency,
        resume=args.resume,
        batch_delay=args.delay,
    ))


if __name__ == "__main__":
    main()
