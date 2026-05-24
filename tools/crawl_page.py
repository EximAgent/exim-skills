"""Crawl a URL via Cloudflare Browser Rendering REST API.

Returns page content as clean markdown — company name, products, navigation links.
Used by the pipeline agent to verify if a URL matches a company.
"""

from __future__ import annotations

import json
import os
import time

import httpx

CF_API_KEY = os.environ.get("CLOUDFLARE_API_KEY", "")
CF_ACCOUNT_ID = os.environ.get("CF_ACCOUNT_ID", "")
CF_CRAWL_URL = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/browser-rendering/crawl"


def crawl_url(url: str, timeout: int = 30) -> dict:
    """Crawl a URL via Cloudflare Browser Rendering.

    Returns:
        {"url": str, "title": str, "markdown": str, "status": int}
        or {"url": str, "error": str} on failure.
    """
    headers = {"Authorization": f"Bearer {CF_API_KEY}", "Content-Type": "application/json"}

    try:
        resp = httpx.post(CF_CRAWL_URL, headers=headers, json={"url": url, "formats": ["markdown"], "limit": 1}, timeout=15.0)
        data = resp.json()
        if data.get("errors"):
            return {"url": url, "error": data["errors"][0].get("message", "unknown")}

        job_id = data.get("result", "")
        if not isinstance(job_id, str) or not job_id:
            return {"url": url, "error": "no job id"}

        for _ in range(timeout * 2):
            time.sleep(0.5)
            resp = httpx.get(f"{CF_CRAWL_URL}/{job_id}", headers=headers, timeout=10.0)
            result = resp.json().get("result", {})
            if result.get("status") in ("completed", "errored"):
                records = result.get("records", [])
                if records:
                    rec = records[0]
                    return {
                        "url": url,
                        "title": rec.get("metadata", {}).get("title", ""),
                        "markdown": rec.get("markdown", ""),
                        "status": rec.get("metadata", {}).get("status", 0),
                    }
                return {"url": url, "error": "no records"}

        return {"url": url, "error": "timeout"}
    except Exception as e:
        return {"url": url, "error": str(e)}


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python -m tools.crawl_page https://example.com")
        sys.exit(1)

    result = crawl_url(sys.argv[1])
    if result.get("error"):
        print(f"Error: {result['error']}")
    else:
        print(f"Title: {result['title']}")
        print(f"Status: {result['status']}")
        print(f"Content ({len(result['markdown'])} chars):")
        print(result["markdown"][:1000])
