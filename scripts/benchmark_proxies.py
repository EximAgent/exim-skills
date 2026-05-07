"""Benchmark Webshare proxies against macmap.org.

Tests:
1. Proxy health — which proxies can establish a macmap.org session
2. Throughput — how many requests/min we can sustain
3. Rate limits — how quickly proxies get throttled

Usage:
    python scripts/benchmark_proxies.py
    python scripts/benchmark_proxies.py --batch-size 30 --throughput-duration 60
"""

import argparse
import asyncio
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://www.macmap.org"
CACHE_DIR = Path.home() / ".cache" / "macmap_crawl"

HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "x-requested-with": "XMLHttpRequest",
    "referer": "https://www.macmap.org/",
}


# ---------------------------------------------------------------------------
# Proxy loading
# ---------------------------------------------------------------------------

def load_proxies() -> list[dict]:
    """Load proxy list from Webshare API. Returns list of proxy dicts."""
    api_key = os.environ.get("WEBSHARE_API_KEY")
    if not api_key:
        print("[error] WEBSHARE_API_KEY not set")
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
                proxies.append({
                    "url": f"http://{p['username']}:{p['password']}@{p['proxy_address']}:{p['port']}",
                    "ip": p["proxy_address"],
                    "port": p["port"],
                    "city": p.get("city_name", ""),
                    "country": p.get("country_code", ""),
                })
        if not data["next"]:
            break
        page += 1

    print(f"[proxy] Loaded {len(proxies)} proxies from Webshare")
    return proxies


# ---------------------------------------------------------------------------
# Phase 1: Health check — test session init on all proxies
# ---------------------------------------------------------------------------

def test_proxy_session(proxy: dict) -> dict:
    """Test if a proxy can establish a macmap.org session. Returns result dict."""
    proxy_url = proxy["url"]
    label = f"{proxy['ip']}:{proxy['port']}"
    start = time.time()

    try:
        session = requests.Session()
        session.headers.update({
            "user-agent": HEADERS["user-agent"],
            "referer": HEADERS["referer"],
        })
        session.proxies = {"http": proxy_url, "https": proxy_url}

        resp = session.get(
            f"{BASE_URL}/en//query/results?reporter=842&partner=004&product=262019&level=6",
            timeout=20,
            allow_redirects=True,
        )
        elapsed = time.time() - start

        if resp.status_code == 200 and "ASP.NET_SessionId" in dict(session.cookies):
            cookies = dict(session.cookies)
            cookies["Culture"] = "en"
            return {
                "proxy": label,
                "url": proxy_url,
                "status": "ok",
                "latency_s": round(elapsed, 2),
                "cookies": cookies,
            }
        else:
            return {
                "proxy": label,
                "url": proxy_url,
                "status": "blocked",
                "latency_s": round(elapsed, 2),
                "http_status": resp.status_code,
            }
    except Exception as e:
        elapsed = time.time() - start
        return {
            "proxy": label,
            "url": proxy_url,
            "status": "error",
            "latency_s": round(elapsed, 2),
            "error": str(e)[:100],
        }


def run_health_check(proxies: list[dict], batch_size: int = 50) -> list[dict]:
    """Test all proxies in parallel batches."""
    print(f"\n{'='*60}")
    print(f"PHASE 1: Proxy Health Check ({len(proxies)} proxies, batch={batch_size})")
    print(f"{'='*60}")

    results = []
    start = time.time()

    for i in range(0, len(proxies), batch_size):
        batch = proxies[i:i + batch_size]
        print(f"  Testing batch {i // batch_size + 1}/{(len(proxies) + batch_size - 1) // batch_size} ({len(batch)} proxies)...")

        with ThreadPoolExecutor(max_workers=batch_size) as executor:
            batch_results = list(executor.map(test_proxy_session, batch))
        results.extend(batch_results)

        ok = sum(1 for r in batch_results if r["status"] == "ok")
        blocked = sum(1 for r in batch_results if r["status"] == "blocked")
        err = sum(1 for r in batch_results if r["status"] == "error")
        print(f"    ok={ok} blocked={blocked} error={err}")

    elapsed = time.time() - start

    ok_proxies = [r for r in results if r["status"] == "ok"]
    blocked_proxies = [r for r in results if r["status"] == "blocked"]
    error_proxies = [r for r in results if r["status"] == "error"]

    avg_latency = sum(r["latency_s"] for r in ok_proxies) / len(ok_proxies) if ok_proxies else 0

    print(f"\n  Health Check Results ({elapsed:.1f}s):")
    print(f"    Working:  {len(ok_proxies)}/{len(proxies)} ({100*len(ok_proxies)/len(proxies):.0f}%)")
    print(f"    Blocked:  {len(blocked_proxies)}")
    print(f"    Errors:   {len(error_proxies)}")
    print(f"    Avg latency (working): {avg_latency:.2f}s")

    return results


# ---------------------------------------------------------------------------
# Phase 2: Throughput test — sustained request rate with working proxies
# ---------------------------------------------------------------------------

async def throughput_request(
    proxy_url: str,
    cookies: dict,
    endpoint: str,
    params: dict,
) -> dict:
    """Make a single API request through a proxy. Returns timing info."""
    url = f"{BASE_URL}{endpoint}"
    start = time.time()

    try:
        async with httpx.AsyncClient(proxy=proxy_url, follow_redirects=True) as client:
            resp = await client.get(url, params=params, headers=HEADERS, cookies=cookies, timeout=30)
            elapsed = time.time() - start
            return {
                "status_code": resp.status_code,
                "elapsed": round(elapsed, 3),
                "ok": resp.status_code == 200,
                "rate_limited": resp.status_code == 403,
            }
    except Exception as e:
        elapsed = time.time() - start
        return {
            "status_code": 0,
            "elapsed": round(elapsed, 3),
            "ok": False,
            "error": str(e)[:80],
        }


async def run_throughput_test(
    working_proxies: list[dict],
    duration_s: int = 60,
    concurrency: int = 10,
) -> dict:
    """Fire requests as fast as possible using working proxies and measure throughput."""
    print(f"\n{'='*60}")
    print(f"PHASE 2: Throughput Test ({len(working_proxies)} proxies, {duration_s}s, concurrency={concurrency})")
    print(f"{'='*60}")

    if not working_proxies:
        print("  No working proxies to test!")
        return {}

    # Test endpoints to cycle through
    test_params_list = [
        {"endpoint": "/api/v2/ntlc-products", "params": {"countryCode": "842", "level": "8", "code": "010121"}},
        {"endpoint": "/api/v2/ntlc-products", "params": {"countryCode": "842", "level": "8", "code": "020110"}},
        {"endpoint": "/api/results/customduties", "params": {"reporter": "842", "partner": "156", "product": "0101210010"}},
        {"endpoint": "/api/v2/ntlc-products", "params": {"countryCode": "704", "level": "8", "code": "030110"}},
        {"endpoint": "/api/results/customduties", "params": {"reporter": "704", "partner": "156", "product": "0301110000"}},
    ]

    semaphore = asyncio.Semaphore(concurrency)
    results = []
    proxy_idx = 0
    request_idx = 0
    start_time = time.time()
    rate_limit_times = {}  # proxy -> first rate limit time

    # Time-bucketed stats
    bucket_size = 10  # seconds per bucket
    buckets = {}

    async def fire_request():
        nonlocal proxy_idx, request_idx
        proxy = working_proxies[proxy_idx % len(working_proxies)]
        proxy_idx += 1
        test = test_params_list[request_idx % len(test_params_list)]
        request_idx += 1

        async with semaphore:
            result = await throughput_request(
                proxy["url"], proxy["cookies"], test["endpoint"], test["params"]
            )
            result["proxy"] = proxy["proxy"]
            elapsed_since_start = time.time() - start_time
            result["time_offset"] = round(elapsed_since_start, 2)

            # Track rate limits per proxy
            if result.get("rate_limited") and proxy["proxy"] not in rate_limit_times:
                rate_limit_times[proxy["proxy"]] = elapsed_since_start

            # Bucket stats
            bucket_key = int(elapsed_since_start // bucket_size) * bucket_size
            if bucket_key not in buckets:
                buckets[bucket_key] = {"ok": 0, "rate_limited": 0, "error": 0, "total": 0}
            buckets[bucket_key]["total"] += 1
            if result["ok"]:
                buckets[bucket_key]["ok"] += 1
            elif result.get("rate_limited"):
                buckets[bucket_key]["rate_limited"] += 1
            else:
                buckets[bucket_key]["error"] += 1

            results.append(result)

    # Fire requests continuously for the duration
    tasks = []
    print(f"  Firing requests for {duration_s}s...")
    while time.time() - start_time < duration_s:
        # Launch batch of requests
        batch = []
        for _ in range(concurrency):
            if time.time() - start_time >= duration_s:
                break
            batch.append(asyncio.create_task(fire_request()))
        if batch:
            await asyncio.gather(*batch)

    total_elapsed = time.time() - start_time
    total_ok = sum(1 for r in results if r["ok"])
    total_rate_limited = sum(1 for r in results if r.get("rate_limited"))
    total_errors = sum(1 for r in results if not r["ok"] and not r.get("rate_limited"))
    rpm = total_ok / total_elapsed * 60 if total_elapsed > 0 else 0

    print(f"\n  Throughput Results ({total_elapsed:.1f}s):")
    print(f"    Total requests:  {len(results)}")
    print(f"    Successful:      {total_ok} ({100*total_ok/len(results):.0f}%)")
    print(f"    Rate limited:    {total_rate_limited}")
    print(f"    Errors:          {total_errors}")
    print(f"    Throughput:      {rpm:.0f} successful req/min")
    print(f"    Avg latency:     {sum(r['elapsed'] for r in results if r['ok']) / max(total_ok,1):.2f}s")

    if rate_limit_times:
        first_rl = min(rate_limit_times.values())
        print(f"    First rate limit: {first_rl:.1f}s in")
        print(f"    Proxies rate-limited: {len(rate_limit_times)}/{len(working_proxies)}")

    # Print time buckets
    print(f"\n  Time buckets ({bucket_size}s each):")
    print(f"    {'Time':>6s}  {'OK':>5s}  {'Limit':>5s}  {'Error':>5s}  {'Total':>5s}  {'OK%':>5s}")
    for t in sorted(buckets.keys()):
        b = buckets[t]
        ok_pct = 100 * b["ok"] / b["total"] if b["total"] else 0
        print(f"    {t:>4.0f}s   {b['ok']:>5d}  {b['rate_limited']:>5d}  {b['error']:>5d}  {b['total']:>5d}  {ok_pct:>4.0f}%")

    return {
        "total_requests": len(results),
        "successful": total_ok,
        "rate_limited": total_rate_limited,
        "errors": total_errors,
        "throughput_rpm": round(rpm, 1),
        "duration_s": round(total_elapsed, 1),
        "first_rate_limit_s": round(min(rate_limit_times.values()), 1) if rate_limit_times else None,
        "proxies_rate_limited": len(rate_limit_times),
        "buckets": buckets,
    }


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------

def print_recommendations(health_results: list, throughput_results: dict):
    working = sum(1 for r in health_results if r["status"] == "ok")
    total = len(health_results)

    print(f"\n{'='*60}")
    print(f"RECOMMENDATIONS")
    print(f"{'='*60}")
    print(f"  Working proxies: {working}/{total}")

    if throughput_results:
        rpm = throughput_results.get("throughput_rpm", 0)
        rl_count = throughput_results.get("proxies_rate_limited", 0)
        first_rl = throughput_results.get("first_rate_limit_s")

        print(f"  Sustained throughput: ~{rpm:.0f} req/min")

        # Estimate crawl time
        # 15 reporters × 5612 HS × 14 partners × ~4 NTLC avg × 4 endpoints
        total_calls = 15 * 5612 * 14 * 4 * 4
        if rpm > 0:
            hours = total_calls / rpm / 60
            print(f"  Estimated total crawl time: {hours:.0f} hours ({hours/24:.1f} days)")

        if first_rl:
            print(f"\n  Rate limit detected at {first_rl:.0f}s")
            if rl_count > working * 0.5:
                print(f"  WARNING: >50% of proxies hit rate limits")
                print(f"  Recommendation: Increase delay to 1-2s, reduce concurrency to 3")
            else:
                print(f"  Only {rl_count}/{working} proxies rate-limited — rotation is effective")

        # Concurrency recommendation
        if rpm > 300:
            print(f"\n  Suggested: --concurrency 10 --delay 0.3")
        elif rpm > 100:
            print(f"\n  Suggested: --concurrency 5 --delay 0.5")
        else:
            print(f"\n  Suggested: --concurrency 3 --delay 1.0")
            print(f"  Consider upgrading to residential proxies for better success rate")

    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Benchmark Webshare proxies against macmap.org")
    parser.add_argument("--batch-size", type=int, default=50, help="Proxies to test in parallel (default: 50)")
    parser.add_argument("--throughput-duration", type=int, default=60, help="Throughput test duration in seconds (default: 60)")
    parser.add_argument("--throughput-concurrency", type=int, default=10, help="Concurrent requests during throughput test (default: 10)")
    parser.add_argument("--skip-throughput", action="store_true", help="Skip throughput test, only do health check")
    args = parser.parse_args()

    proxies = load_proxies()
    if not proxies:
        print("[error] No proxies loaded")
        return

    # Phase 1: Health check
    health_results = run_health_check(proxies, batch_size=args.batch_size)

    # Save health results
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    working_proxies = [r for r in health_results if r["status"] == "ok"]

    # Phase 2: Throughput test
    throughput_results = {}
    if not args.skip_throughput and working_proxies:
        throughput_results = asyncio.run(run_throughput_test(
            working_proxies,
            duration_s=args.throughput_duration,
            concurrency=args.throughput_concurrency,
        ))

    # Print recommendations
    print_recommendations(health_results, throughput_results)

    # Save full results
    benchmark = {
        "timestamp": int(time.time()),
        "total_proxies": len(proxies),
        "working_proxies": len(working_proxies),
        "blocked_proxies": sum(1 for r in health_results if r["status"] == "blocked"),
        "error_proxies": sum(1 for r in health_results if r["status"] == "error"),
        "working_proxy_list": [r["proxy"] for r in working_proxies],
        "avg_latency_s": round(
            sum(r["latency_s"] for r in working_proxies) / len(working_proxies), 2
        ) if working_proxies else 0,
        "throughput": {
            k: v for k, v in throughput_results.items()
            if k != "buckets"
        } if throughput_results else {},
    }

    out_path = CACHE_DIR / "proxy_benchmark.json"
    with open(out_path, "w") as f:
        json.dump(benchmark, f, indent=2)
    print(f"[saved] Results written to {out_path}")


if __name__ == "__main__":
    main()
