"""Google Search via Serper API.

Returns raw candidates with directory/aggregator sites filtered out.
The agent reads titles, snippets, and URLs to decide which are real company websites.
"""

from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

import httpx
from tqdm import tqdm

SERPER_API_URL = "https://google.serper.dev/search"
SERPER_API_KEY = os.environ.get("SERPER_API_KEY", "")

DIRECTORY_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "tiktok.com",
    "bloomberg.com", "dnb.com", "importgenius.com",
    "yellowpages.com", "yelp.com", "bbb.org",
    "crunchbase.com", "zoominfo.com", "pitchbook.com",
    "glassdoor.com", "indeed.com", "wikipedia.org",
    "panjiva.com", "importyeti.com", "trademap.org",
    "alibaba.com", "globalsources.com", "thomasnet.com",
    "amazon.com", "ebay.com",
    "volza.com", "tendata.com", "seair.co.in",
    "emis.com", "exportgenius.in", "eximpedia.app",
    "trademo.com", "thetradevision.com", "veritradecorp.com",
    "nbd.ltd", "informacolombia.com", "datacreditoempresas.com.co",
}


def is_directory_url(url: str) -> bool:
    try:
        domain = urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return True
    return any(domain.endswith(d) for d in DIRECTORY_DOMAINS)


def extract_homepage(url: str) -> str:
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        return url


async def search_serper(query: str, semaphore: asyncio.Semaphore, pbar_cost: dict) -> dict:
    """Search Google via Serper API. Returns raw candidates with directories filtered."""
    async with semaphore:
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    SERPER_API_URL,
                    headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
                    json={"q": query, "num": 10},
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            return {"candidates": [], "query": query, "error": str(e)}

        pbar_cost["total"] += 0.003

        candidates = []
        for i, result in enumerate(data.get("organic", [])):
            link = result.get("link", "")
            if is_directory_url(link):
                continue
            candidates.append({
                "url": extract_homepage(link),
                "full_url": link,
                "title": result.get("title", ""),
                "snippet": result.get("snippet", ""),
                "position": i + 1,
            })

        return {"candidates": candidates, "query": query}


async def search_serper_batch(queries: list[tuple[str, str]], concurrency: int = 5) -> list[dict]:
    """Search Google for multiple (query, company_name) pairs with cost tracking."""
    semaphore = asyncio.Semaphore(concurrency)
    pbar_cost = {"total": 0.0}
    results = [None] * len(queries)
    pbar = tqdm(total=len(queries), desc="search_google")

    async def _run(idx, query, _name):
        result = await search_serper(query, semaphore, pbar_cost)
        results[idx] = result
        pbar.set_postfix(cost=f"${pbar_cost['total']:.4f}")
        pbar.update(1)

    await asyncio.gather(*[_run(i, q, n) for i, (q, n) in enumerate(queries)])
    pbar.close()
    return results


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 3:
        print('Usage: python -m tools.search "search query" "company name"')
        sys.exit(1)

    result = asyncio.run(search_serper(sys.argv[1], asyncio.Semaphore(1), {"total": 0.0}))
    print(json.dumps(result, indent=2))
