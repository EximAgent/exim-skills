"""One-time script: fetch countries from macmap.org and index into Typesense.

Usage:
    python scripts/fetch_macmap_countries.py
"""

import os
import sys

from dotenv import load_dotenv
import httpx

load_dotenv()
import typesense
from typesense.exceptions import ObjectNotFound

COLLECTION_NAME = os.environ.get("TYPESENSE_MACMAP_COUNTRIES_COLLECTION", "macmap_countries")

SCHEMA = {
    "name": COLLECTION_NAME,
    "fields": [
        {"name": "code", "type": "string", "facet": True},
        {"name": "name", "type": "string"},
        {"name": "iso2", "type": "string", "facet": True},
        {"name": "iso3", "type": "string", "facet": True},
    ],
}


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


def fetch_countries() -> list[dict]:
    """Fetch country list from macmap.org API."""
    url = "https://www.macmap.org/api/countries"
    headers = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json; charset=utf-8",
        "referer": "https://www.macmap.org/",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "x-requested-with": "XMLHttpRequest",
    }
    cookies = {"Culture": "en"}

    # First visit the main page to get session cookies (ASP.NET_SessionId, F5 BIG-IP cookie)
    print(f"[fetch] Establishing session with macmap.org ...")
    with httpx.Client(headers=headers, cookies=cookies, timeout=60, follow_redirects=True, http2=True) as client:
        landing = client.get("https://www.macmap.org/")
        print(f"[fetch] Landing page status: {landing.status_code}")

        print(f"[fetch] Fetching countries from {url} ...")
        resp = client.get(url)
        resp.raise_for_status()
        data = resp.json()
        print(f"[fetch] Got {len(data)} countries")
        return data


def index_into_typesense(records: list[dict]) -> None:
    """Create collection and bulk-import country records."""
    client = get_typesense_client()

    try:
        client.collections[COLLECTION_NAME].delete()
        print(f"[db] Dropped existing collection '{COLLECTION_NAME}'")
    except ObjectNotFound:
        pass

    client.collections.create(SCHEMA)
    print(f"[db] Created collection '{COLLECTION_NAME}'")

    docs = []
    for r in records:
        docs.append({
            "id": r["Code"],
            "code": r["Code"],
            "name": r["Name"],
            "iso2": r.get("ISO2", ""),
            "iso3": r.get("ISO3", ""),
        })

    print(f"[db] Importing {len(docs)} documents ...")
    results = client.collections[COLLECTION_NAME].documents.import_(docs, {"action": "upsert"})

    success = sum(1 for r in results if r.get("success", True))
    failed = len(results) - success
    print(f"[db] Imported {success} documents ({failed} failures)")


if __name__ == "__main__":
    records = fetch_countries()
    index_into_typesense(records)
    print("[done] Countries indexed successfully")
