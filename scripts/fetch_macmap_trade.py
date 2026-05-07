"""Crawl macmap.org trade data and index into Typesense.

Flow:
1. For each reporter + HS subheading code → fetch NTLC (national tariff line)
   product codes via /api/v2/ntlc-products
2. For each NTLC code + partner → fetch tariffs, trade remedies, NTM measures,
   and custom duties via /api/results/* endpoints
3. Index results into Typesense

Usage:
    # Recommended: crawl all default reporters with default settings
    python scripts/fetch_macmap_trade.py --resume

    # Crawl specific reporters
    python scripts/fetch_macmap_trade.py --reporters 842,704,356 --resume

    # Limit total combos (for testing)
    python scripts/fetch_macmap_trade.py --reporters 842 --limit 100

    # Retry only previously failed combos from failed_combos.jsonl
    python scripts/fetch_macmap_trade.py --retry-failed

    # Skip proxy warmup (faster startup, lazy init)
    python scripts/fetch_macmap_trade.py --no-proxy-warmup --resume

Best settings (from benchmarking):
- --concurrency 30 (default)
- All 14 default partners (don't restrict --partners)
- ~45 combos/min sustained throughput on a single machine
- Run multiple machines with different --reporters to scale linearly
"""

import argparse
import asyncio
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
import httpx
import requests

load_dotenv()
import typesense
from typesense.exceptions import ObjectNotFound

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

COLLECTION_NAME = os.environ.get("TYPESENSE_MACMAP_TRADE_COLLECTION", "macmap_trade")

CACHE_DIR = Path.home() / ".cache" / "macmap_crawl"
CHECKPOINT_FILE = CACHE_DIR / "checkpoint.json"
FAILED_LOG_FILE = CACHE_DIR / "failed_combos.jsonl"

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
# Cookie management
# ---------------------------------------------------------------------------

def get_fresh_cookies_sync(proxy_url: str | None = None) -> dict[str, str]:
    """Get session cookies for macmap.org by visiting a results page.

    Args:
        proxy_url: Optional proxy URL (http://user:pass@ip:port) to route through.

    Returns:
        Dict of cookies including ASP.NET_SessionId needed for API access.
        Empty dict on failure.
    """
    manual_cookies = os.environ.get("MACMAP_COOKIES")
    if manual_cookies:
        cookies = {}
        for part in manual_cookies.split(";"):
            part = part.strip()
            if "=" in part:
                k, v = part.split("=", 1)
                cookies[k.strip()] = v.strip()
        cookies["Culture"] = "en"
        return cookies

    session = requests.Session()
    session.headers.update({
        "user-agent": HEADERS["user-agent"],
        "referer": "https://www.macmap.org/",
    })
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}

    proxy_label = proxy_url.split("@")[-1] if proxy_url else "direct"
    try:
        resp = session.get(
            f"{BASE_URL}/en//query/results?reporter=842&partner=004&product=262019&level=6",
            timeout=25,
            allow_redirects=True,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[cookie] Failed via {proxy_label}: {e}")
        return {}

    cookies = dict(session.cookies)
    cookies["Culture"] = "en"
    return cookies


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
    key = f"{reporter}_{partner}_{product}"
    return key in checkpoint["completed"]


def mark_completed(checkpoint: dict, reporter: str, partner: str, product: str) -> None:
    key = f"{reporter}_{partner}_{product}"
    checkpoint["completed"][key] = True


# ---------------------------------------------------------------------------
# Failure logging
# ---------------------------------------------------------------------------

def log_failure(
    reporter: str,
    partner: str,
    product: str,
    hs_code: str,
    error: str,
    proxy: str | None = None,
) -> None:
    """Append a failed combo to the JSONL failure log for later retry."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "reporter": reporter,
        "partner": partner,
        "product": product,
        "hs_code": hs_code,
        "error": error,
        "proxy": (proxy.split("@")[-1] if proxy else "direct"),
        "timestamp": int(time.time()),
    }
    with open(FAILED_LOG_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def load_failed_combos() -> list[dict]:
    """Load unique failed combos from the JSONL log, deduplicating."""
    if not FAILED_LOG_FILE.exists():
        print("[retry] No failed_combos.jsonl found")
        return []
    seen = set()
    combos = []
    with open(FAILED_LOG_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            key = f"{entry['reporter']}_{entry['partner']}_{entry['product']}"
            if key not in seen:
                seen.add(key)
                combos.append(entry)
    print(f"[retry] Loaded {len(combos)} unique failed combos from {FAILED_LOG_FILE}")
    return combos


def clear_failed_log() -> None:
    """Clear the failure log (called after successful retry)."""
    if FAILED_LOG_FILE.exists():
        FAILED_LOG_FILE.unlink()


# ---------------------------------------------------------------------------
# Proxy management
# ---------------------------------------------------------------------------

def load_proxies() -> list[str]:
    """Load proxy list from Webshare API. Returns list of http://user:pass@ip:port URLs."""
    api_key = os.environ.get("WEBSHARE_API_KEY")
    if not api_key:
        print("[proxy] No WEBSHARE_API_KEY set, running without proxies")
        return []

    proxies = []
    page = 1
    while True:
        resp = requests.get(
            "https://proxy.webshare.io/api/v2/proxy/list/",
            params={"mode": "direct", "page": page, "page_size": 100},
            headers={"Authorization": f"Token {api_key}"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        for p in data["results"]:
            if p["valid"]:
                proxies.append(
                    f"http://{p['username']}:{p['password']}@{p['proxy_address']}:{p['port']}"
                )
        if not data["next"]:
            break
        page += 1

    print(f"[proxy] Loaded {len(proxies)} proxies from Webshare")
    return proxies


def warmup_proxies(proxies: list[str], batch_size: int = 50) -> tuple[list[str], dict[str, dict]]:
    """Test all proxies in parallel and return only working ones with their cookies.

    Returns:
        (working_proxy_urls, {proxy_url: cookies_dict})
    """
    if not proxies:
        return [], {}

    print(f"[warmup] Testing {len(proxies)} proxies (batch={batch_size}) ...")

    def test_one(proxy_url: str) -> tuple[str, dict]:
        cookies = get_fresh_cookies_sync(proxy_url)
        return proxy_url, cookies

    all_cookies = {}
    working = []
    blocked = 0

    for i in range(0, len(proxies), batch_size):
        batch = proxies[i:i + batch_size]
        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            results = list(executor.map(test_one, batch))

        for proxy_url, cookies in results:
            if cookies:
                working.append(proxy_url)
                all_cookies[proxy_url] = cookies
            else:
                blocked += 1

        print(f"  Batch {i // batch_size + 1}: +{sum(1 for _, c in results if c)} working, +{sum(1 for _, c in results if not c)} blocked")

    print(f"[warmup] Result: {len(working)}/{len(proxies)} working ({blocked} blocked)")
    return working, all_cookies


class ProxyRotator:
    """Round-robin proxy rotator with per-proxy session cookies and failure tracking."""

    def __init__(
        self,
        proxies: list[str],
        pre_cookies: dict[str, dict] | None = None,
        max_consecutive_failures: int = 3,
    ):
        self._proxies = proxies
        self._cookies: dict[str, dict[str, str]] = pre_cookies or {}
        self._dead: set[str] = set()
        self._consecutive_failures: dict[str, int] = {}
        self._max_failures = max_consecutive_failures
        self._stats: dict[str, dict] = {}  # proxy -> {requests, successes, failures}
        self._clients: dict[str, httpx.AsyncClient] = {}  # proxy_url -> reusable client
        self._index = 0
        self._lock = asyncio.Lock()

    def get_client(self, proxy_url: str | None) -> httpx.AsyncClient:
        """Return a long-lived AsyncClient for the given proxy, creating if needed.

        Reusing clients avoids TCP+TLS handshake on every request (huge speedup
        when going through a proxy CONNECT tunnel).
        """
        key = proxy_url or "__direct__"
        client = self._clients.get(key)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                proxy=proxy_url,
                follow_redirects=True,
                timeout=httpx.Timeout(45.0, connect=30.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
            self._clients[key] = client
        return client

    async def close_all_clients(self) -> None:
        """Close all cached httpx clients (call at shutdown)."""
        for client in self._clients.values():
            try:
                await client.aclose()
            except Exception:
                pass
        self._clients.clear()

    @property
    def working_count(self) -> int:
        return len(self._proxies) - len(self._dead)

    @property
    def stats_summary(self) -> str:
        total_req = sum(s.get("requests", 0) for s in self._stats.values())
        total_ok = sum(s.get("successes", 0) for s in self._stats.values())
        total_fail = sum(s.get("failures", 0) for s in self._stats.values())
        return f"proxies={self.working_count}/{len(self._proxies)} reqs={total_req} ok={total_ok} fail={total_fail}"

    def record_success(self, proxy_url: str | None) -> None:
        key = proxy_url or "__direct__"
        self._consecutive_failures[key] = 0
        stats = self._stats.setdefault(key, {"requests": 0, "successes": 0, "failures": 0})
        stats["requests"] += 1
        stats["successes"] += 1

    def record_failure(self, proxy_url: str | None) -> None:
        key = proxy_url or "__direct__"
        self._consecutive_failures[key] = self._consecutive_failures.get(key, 0) + 1
        stats = self._stats.setdefault(key, {"requests": 0, "successes": 0, "failures": 0})
        stats["requests"] += 1
        stats["failures"] += 1
        if proxy_url and self._consecutive_failures[key] >= self._max_failures:
            label = proxy_url.split("@")[-1]
            print(f"[proxy] Marking {label} as dead after {self._max_failures} consecutive failures")
            self._dead.add(proxy_url)

    async def next(self) -> tuple[str | None, dict[str, str]]:
        """Return (proxy_url, cookies) for the next working proxy in rotation."""
        if not self._proxies:
            if "__direct__" not in self._cookies:
                self._cookies["__direct__"] = get_fresh_cookies_sync(None)
            return None, self._cookies["__direct__"]

        for _ in range(len(self._proxies)):
            async with self._lock:
                proxy = self._proxies[self._index % len(self._proxies)]
                self._index += 1

            if proxy in self._dead:
                continue

            if proxy not in self._cookies:
                cookies = get_fresh_cookies_sync(proxy)
                if not cookies:
                    self._dead.add(proxy)
                    continue
                self._cookies[proxy] = cookies

            return proxy, self._cookies[proxy]

        print("[proxy] All proxies failed, falling back to direct connection")
        if "__direct__" not in self._cookies:
            self._cookies["__direct__"] = get_fresh_cookies_sync(None)
        return None, self._cookies["__direct__"]

    def refresh_cookies(self, proxy_url: str | None) -> dict[str, str]:
        """Force-refresh cookies for a specific proxy."""
        key = proxy_url or "__direct__"
        cookies = get_fresh_cookies_sync(proxy_url)
        if not cookies and proxy_url:
            self._dead.add(proxy_url)
            return {}
        self._cookies[key] = cookies
        return cookies


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
    max_retries: int = 1,
) -> tuple[str, dict | list | None, bool]:
    """Fetch one API endpoint. Returns (endpoint_name, data, needs_cookie_refresh).

    Retries connection-level errors once. Does NOT retry 5xx (macmap-side errors
    aren't fixed by retrying — they just slow the crawl down).
    """
    url = f"{BASE_URL}{API_ENDPOINTS[endpoint]}"
    params = {"reporter": reporter, "partner": partner, "product": product}

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(url, params=params, headers=HEADERS, cookies=cookies)
            if resp.status_code == 403:
                return endpoint, None, True
            if 500 <= resp.status_code < 600:
                last_err = f"HTTP {resp.status_code}"
                break
            resp.raise_for_status()
            data = resp.json()
            return endpoint, data, False
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            last_err = e
            if attempt < max_retries:
                await asyncio.sleep(0.3)
                continue
            break
        except Exception as e:
            last_err = e
            break

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
    max_retries: int = 1,
) -> tuple[list[str], bool]:
    """Fetch national tariff line codes for a reporter + HS subheading.

    Returns (list_of_ntlc_codes, needs_cookie_refresh).
    """
    url = f"{BASE_URL}/api/v2/ntlc-products"
    params = {"countryCode": reporter, "level": "8", "code": hs_code}

    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(url, params=params, headers=HEADERS, cookies=cookies)
            if resp.status_code == 403:
                return [], True
            if 500 <= resp.status_code < 600:
                last_err = f"HTTP {resp.status_code}"
                break
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                codes = [item.get("code") or item.get("Code") or item.get("ProductCode", "") for item in data]
                return [c for c in codes if c], False
            return [], False
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            last_err = e
            if attempt < max_retries:
                await asyncio.sleep(0.3)
                continue
            break
        except Exception as e:
            last_err = e
            break

    print(f"  [error] ntlc-products {reporter}/{hs_code}: {type(last_err).__name__}: {last_err}")
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

    doc["has_taxes"] = has_taxes
    if has_taxes:
        doc["taxes_json"] = json.dumps(taxes_data)
        summary_parts.append("Has applicable taxes")

    if has_remedies:
        remedy_types = list({r.get("RemedyType", "") for r in remedies_data["TradeRemedyData"] if r.get("RemedyType")})
        doc["trade_remedy_types"] = remedy_types
        doc["trade_remedies_json"] = json.dumps(remedies_data)
        summary_parts.append(f"Trade remedies: {', '.join(remedy_types)}")

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
    proxy_warmup: bool = True,
    max_proxy_failures: int = 3,
    retry_failed: bool = False,
) -> None:
    """Main crawl function."""

    # Load reference data
    countries = load_countries()
    if not countries:
        print("[error] No countries loaded. Run fetch_macmap_countries.py first.")
        sys.exit(1)

    # Load or reset checkpoint
    if resume or retry_failed:
        checkpoint = load_checkpoint()
        print(f"[resume] Loaded checkpoint: {checkpoint['stats']}")
    else:
        checkpoint = {"completed": {}, "stats": {"total": 0, "indexed": 0, "empty": 0, "errors": 0}}

    # Setup Typesense
    ts_client = get_typesense_client()
    ensure_collection(ts_client)

    # Load and warm up proxies
    raw_proxies = load_proxies()
    if proxy_warmup and raw_proxies:
        working_proxies, pre_cookies = warmup_proxies(raw_proxies)
        if not working_proxies:
            print("[error] No working proxies found! Check your proxy plan or try --no-proxy-warmup")
            sys.exit(1)
        proxy_rotator = ProxyRotator(working_proxies, pre_cookies, max_proxy_failures)
    else:
        proxy_rotator = ProxyRotator(raw_proxies, max_consecutive_failures=max_proxy_failures)

    # NTLC code cache
    ntlc_cache: dict[str, list[str]] = {}
    NTLC_CACHE_FILE = CACHE_DIR / "ntlc_cache.json"
    if (resume or retry_failed) and NTLC_CACHE_FILE.exists():
        with open(NTLC_CACHE_FILE) as f:
            ntlc_cache = json.load(f)
        print(f"[resume] Loaded {len(ntlc_cache)} cached NTLC lookups")

    # Crawl state — separate semaphores so NTLC lookups (producer) don't starve
    # combo workers (consumers). The combo semaphore is the main throttle.
    semaphore = asyncio.Semaphore(concurrency)  # combo processing
    ntlc_semaphore = asyncio.Semaphore(max(concurrency // 2, 5))  # NTLC lookups
    batch_docs = []
    batch_count = 0
    combo_count = 0
    crawl_start_time = time.time()

    def flush_batch() -> None:
        """Upsert batch docs to Typesense, save checkpoint and NTLC cache."""
        if batch_docs:
            try:
                ts_client.collections[COLLECTION_NAME].documents.import_(
                    batch_docs, {"action": "upsert"}
                )
            except Exception as e:
                print(f"  [error] Batch upsert failed: {e}")
            batch_docs.clear()

        save_checkpoint(checkpoint)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with open(NTLC_CACHE_FILE, "w") as f:
            json.dump(ntlc_cache, f)

    def print_progress() -> None:
        stats = checkpoint["stats"]
        elapsed = time.time() - crawl_start_time
        rate = stats["total"] / elapsed * 60 if elapsed > 0 else 0
        print(
            f"[progress] {stats['total']} processed | "
            f"indexed={stats['indexed']} empty={stats['empty']} errors={stats['errors']} | "
            f"{rate:.0f} combos/min | {proxy_rotator.stats_summary}"
        )

    async def get_ntlc_codes(reporter: str, hs_code: str) -> list[str]:
        """Get NTLC codes, using cache when available."""
        cache_key = f"{reporter}_{hs_code}"
        if cache_key in ntlc_cache:
            return ntlc_cache[cache_key]

        async with ntlc_semaphore:
            proxy_url, cookies = await proxy_rotator.next()
            client = proxy_rotator.get_client(proxy_url)
            codes, needs_refresh = await fetch_ntlc_products(
                client, reporter, hs_code, cookies
            )
            if needs_refresh:
                cookies = proxy_rotator.refresh_cookies(proxy_url)
                if cookies:
                    codes, _ = await fetch_ntlc_products(
                        client, reporter, hs_code, cookies
                    )
                else:
                    proxy_rotator.record_failure(proxy_url)
            else:
                proxy_rotator.record_success(proxy_url)
            ntlc_cache[cache_key] = codes
            if batch_delay > 0:
                await asyncio.sleep(batch_delay)
            return codes

    async def process_combo(
        reporter: str, partner: str, ntlc_code: str, hs_code: str,
    ) -> None:
        nonlocal batch_count, combo_count

        if is_completed(checkpoint, reporter, partner, ntlc_code):
            return

        async with semaphore:
            proxy_url, cookies = await proxy_rotator.next()
            client = proxy_rotator.get_client(proxy_url)
            results, needs_refresh = await fetch_all_endpoints(
                client, reporter, partner, ntlc_code, cookies
            )

            if needs_refresh:
                cookies = proxy_rotator.refresh_cookies(proxy_url)
                if cookies:
                    results, still_needs = await fetch_all_endpoints(
                        client, reporter, partner, ntlc_code, cookies
                    )
                    if still_needs:
                        proxy_rotator.record_failure(proxy_url)
                        checkpoint["stats"]["errors"] += 1
                        log_failure(reporter, partner, ntlc_code, hs_code, "403_after_refresh", proxy_url)
                        return
                else:
                    proxy_rotator.record_failure(proxy_url)
                    checkpoint["stats"]["errors"] += 1
                    log_failure(reporter, partner, ntlc_code, hs_code, "cookie_refresh_failed", proxy_url)
                    return

            proxy_rotator.record_success(proxy_url)

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

            if batch_count % 100 == 0:
                flush_batch()
                print_progress()

            if batch_delay > 0:
                await asyncio.sleep(batch_delay)

    # ---------------------------------------------------------------------------
    # Retry-failed mode: re-crawl only combos from failed_combos.jsonl
    # ---------------------------------------------------------------------------
    if retry_failed:
        failed_combos = load_failed_combos()
        if not failed_combos:
            print("[retry] Nothing to retry")
            return

        # Filter out already-completed combos
        to_retry = [
            c for c in failed_combos
            if not is_completed(checkpoint, c["reporter"], c["partner"], c["product"])
        ]
        print(f"[retry] {len(to_retry)} combos to retry ({len(failed_combos) - len(to_retry)} already completed)")

        if limit:
            to_retry = to_retry[:limit]

        tasks = [
            process_combo(c["reporter"], c["partner"], c["product"], c["hs_code"])
            for c in to_retry
        ]
        if tasks:
            # Process in batches to avoid overwhelming
            for i in range(0, len(tasks), concurrency * 2):
                batch = tasks[i:i + concurrency * 2]
                await asyncio.gather(*batch)

        flush_batch()
        await proxy_rotator.close_all_clients()
        stats = checkpoint["stats"]
        print(f"\n[done] Retry complete: indexed={stats['indexed']} empty={stats['empty']} errors={stats['errors']}")
        print(f"[done] {proxy_rotator.stats_summary}")

        # Clear the failed log if no errors this run
        remaining_errors = stats["errors"]
        if remaining_errors == 0:
            clear_failed_log()
            print("[done] Cleared failed_combos.jsonl (all retries succeeded)")
        else:
            print(f"[done] {remaining_errors} errors remain — run --retry-failed again")
        return

    # ---------------------------------------------------------------------------
    # Normal crawl mode
    # ---------------------------------------------------------------------------
    hs_codes = load_hs_products()
    if not hs_codes:
        print("[error] No HS codes loaded. Run fetch_hscodes.py first.")
        sys.exit(1)

    partner_list = partners if partners else list(DEFAULT_REPORTERS)

    for r in reporters:
        if r not in countries:
            print(f"[warn] Reporter code {r} not found in countries list")
    for p in partner_list:
        if p not in countries:
            print(f"[warn] Partner code {p} not found in countries list")

    print(f"[config] {len(reporters)} reporters x {len(hs_codes)} HS codes x {len(partner_list)} partners")

    # Pipelined producer/consumer: NTLC lookups feed a queue, workers consume combos.
    # This decouples NTLC discovery from combo processing so both run concurrently.
    combo_queue: asyncio.Queue = asyncio.Queue(maxsize=concurrency * 10)
    DONE_SENTINEL = object()

    async def ntlc_producer(reporter: str) -> int:
        """Iterate HS codes, fetch NTLC in parallel, push combos to queue."""
        nonlocal combo_count
        produced = 0
        # Fetch NTLC codes in parallel batches to keep the queue fed
        ntlc_batch_size = concurrency
        for batch_start in range(0, len(hs_codes), ntlc_batch_size):
            if limit and combo_count >= limit:
                break
            hs_batch = hs_codes[batch_start:batch_start + ntlc_batch_size]
            ntlc_tasks = [get_ntlc_codes(reporter, hs) for hs in hs_batch]
            ntlc_results = await asyncio.gather(*ntlc_tasks)

            for hs_code, ntlc_codes in zip(hs_batch, ntlc_results):
                if not ntlc_codes:
                    continue
                for ntlc_code in ntlc_codes:
                    for partner in partner_list:
                        if partner == reporter:
                            continue
                        if limit and combo_count + produced >= limit:
                            return produced
                        await combo_queue.put((reporter, partner, ntlc_code, hs_code))
                        produced += 1
        return produced

    async def combo_worker() -> None:
        """Consume combos from the queue and process them. Exits on DONE_SENTINEL."""
        while True:
            item = await combo_queue.get()
            if item is DONE_SENTINEL:
                combo_queue.task_done()
                break
            reporter, partner, ntlc_code, hs_code = item
            try:
                await process_combo(reporter, partner, ntlc_code, hs_code)
            finally:
                combo_queue.task_done()

    stopped = False
    for reporter in reporters:
        if stopped or (limit and combo_count >= limit):
            break
        print(f"\n[crawl] Reporter: {countries.get(reporter, reporter)} ({reporter})")

        # Spawn workers + producer concurrently
        # Workers = concurrency (each holds 1 semaphore slot during combo processing)
        workers = [asyncio.create_task(combo_worker()) for _ in range(concurrency)]
        producer = asyncio.create_task(ntlc_producer(reporter))

        # Wait for producer to finish enqueueing all combos
        produced = await producer
        # Send sentinel for each worker to signal end
        for _ in range(concurrency):
            await combo_queue.put(DONE_SENTINEL)
        # Wait for workers to drain
        await asyncio.gather(*workers)

        if limit and combo_count >= limit:
            stopped = True

    # Final flush
    flush_batch()
    await proxy_rotator.close_all_clients()

    stats = checkpoint["stats"]
    elapsed = time.time() - crawl_start_time
    print(f"\n[done] Crawl complete in {elapsed/60:.1f} min")
    print(f"[done] indexed={stats['indexed']} empty={stats['empty']} errors={stats['errors']}")
    print(f"[done] NTLC cache: {len(ntlc_cache)} lookups cached")
    print(f"[done] {proxy_rotator.stats_summary}")

    if stats["errors"] > 0:
        print(f"[done] {stats['errors']} failures logged to {FAILED_LOG_FILE}")
        print(f"[done] Run with --retry-failed to retry them")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Crawl macmap.org trade data into Typesense")
    parser.add_argument(
        "--reporters", type=str, default=None,
        help="Comma-separated reporter country codes (default: shortlist of 15)",
    )
    parser.add_argument(
        "--partners", type=str, default=None,
        help="Comma-separated partner country codes (default: same as reporters list)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Max combos to crawl (for testing)")
    parser.add_argument("--concurrency", type=int, default=30, help="Max concurrent combos (default: 30, best with all default partners)")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--delay", type=float, default=0.0, help="Optional sleep between requests in seconds (default: 0, semaphore handles throttling)")
    parser.add_argument("--retry-failed", action="store_true", help="Retry only previously failed combos from failed_combos.jsonl")
    parser.add_argument("--no-proxy-warmup", action="store_true", help="Skip proxy pre-testing (use lazy init)")
    parser.add_argument("--max-proxy-failures", type=int, default=3, help="Consecutive failures before marking proxy dead (default: 3)")

    args = parser.parse_args()

    reporters = args.reporters.split(",") if args.reporters else DEFAULT_REPORTERS
    partners = args.partners.split(",") if args.partners else None

    print(f"[config] Reporters: {reporters}")
    print(f"[config] Partners: {partners or 'same as reporters'}")
    print(f"[config] Concurrency: {args.concurrency}, Delay: {args.delay}s")
    if args.limit:
        print(f"[config] Limit: {args.limit}")
    if args.retry_failed:
        print(f"[config] Mode: RETRY FAILED")

    asyncio.run(crawl(
        reporters=reporters,
        partners=partners,
        limit=args.limit,
        concurrency=args.concurrency,
        resume=args.resume,
        batch_delay=args.delay,
        proxy_warmup=not args.no_proxy_warmup,
        max_proxy_failures=args.max_proxy_failures,
        retry_failed=args.retry_failed,
    ))


if __name__ == "__main__":
    main()
