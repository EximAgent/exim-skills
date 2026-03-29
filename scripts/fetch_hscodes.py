"""One-time script: fetch HS codes from macmap.org and index into Typesense.

Usage:
    python scripts/fetch_hscodes.py

Fetches ~7K HS code records (H6 revision) and creates a Typesense collection
'hscodes' with full parent-chain breadcrumbs and Gemini embeddings for
hybrid (lexical + semantic) search.
"""

import asyncio
import json
import os
import sys

import httpx
import typesense
from typesense.exceptions import ObjectAlreadyExists, ObjectNotFound

# Add skills root for shared modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".claude", "skills"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".claude", "skills", "extract-company-info", "scripts"))

from embeddings import EMBEDDING_DIM, generate_embeddings_batch_gemini

COLLECTION_NAME = os.environ.get("TYPESENSE_HSCODES_COLLECTION", "hscodes")

HSCODES_SCHEMA = {
    "name": COLLECTION_NAME,
    "fields": [
        {"name": "code", "type": "string", "facet": True},
        {"name": "name", "type": "string"},
        {"name": "parent_code", "type": "string", "optional": True},
        {"name": "level", "type": "string", "facet": True},
        {"name": "full_path", "type": "string"},
        {"name": "embedding", "type": "float[]", "num_dim": EMBEDDING_DIM, "optional": True},
    ],
    "token_separators": ["-", "."],
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


def classify_level(code: str) -> str:
    """Determine HS code level from code format."""
    if code.isalpha() or (len(code) <= 4 and not code.isdigit()):
        return "section"
    n = len(code)
    if n == 2:
        return "chapter"
    if n == 4:
        return "heading"
    return "subheading"


def build_full_paths(records: list[dict]) -> dict[str, str]:
    """Build breadcrumb paths by walking parent chains."""
    by_code = {r["Code"]: r["Name"] for r in records}
    parent_map = {r["Code"]: r["ParentCode"] for r in records}
    cache: dict[str, str] = {}

    def get_path(code: str) -> str:
        if code in cache:
            return cache[code]
        parent = parent_map.get(code)
        name = by_code.get(code, code)
        if not parent or parent not in by_code:
            cache[code] = name
        else:
            cache[code] = f"{get_path(parent)} > {name}"
        return cache[code]

    for r in records:
        get_path(r["Code"])
    return cache


def fetch_hscodes() -> list[dict]:
    """Fetch HS codes from macmap.org API."""
    url = "https://www.macmap.org/api/products-by-latest-hs-rev?revCode=H6"
    headers = {
        "accept": "application/json, text/javascript, */*; q=0.01",
        "content-type": "application/json; charset=utf-8",
        "referer": "https://www.macmap.org/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "x-requested-with": "XMLHttpRequest",
    }
    cookies = {
        "Culture": "en",
    }

    print(f"[fetch] Fetching HS codes from {url} ...")
    resp = httpx.get(url, headers=headers, cookies=cookies, timeout=60, follow_redirects=True)
    resp.raise_for_status()
    data = resp.json()
    print(f"[fetch] Got {len(data)} records")
    return data


async def index_into_typesense(records: list[dict]) -> None:
    """Create collection, generate embeddings, and bulk-import HS code records."""
    client = get_typesense_client()

    # Drop and recreate collection
    try:
        client.collections[COLLECTION_NAME].delete()
        print(f"[db] Dropped existing collection '{COLLECTION_NAME}'")
    except ObjectNotFound:
        pass

    client.collections.create(HSCODES_SCHEMA)
    print(f"[db] Created collection '{COLLECTION_NAME}' (embedding dim={EMBEDDING_DIM})")

    # Build full paths
    full_paths = build_full_paths(records)

    # Prepare documents (without embeddings first)
    docs = []
    embed_texts = []
    for r in records:
        code = r["Code"]
        name = r["Name"]
        fp = full_paths.get(code, name)
        docs.append({
            "id": code,
            "code": code,
            "name": name,
            "parent_code": r.get("ParentCode") or "",
            "level": classify_level(code),
            "full_path": fp,
        })
        # Embedding text: code + name + full breadcrumb path
        embed_texts.append(f"HS code {code}: {name}. Classification path: {fp}")

    # Generate embeddings in batches
    print(f"[embed] Generating embeddings for {len(embed_texts)} records ...")
    embeddings = await generate_embeddings_batch_gemini(embed_texts, use_cache=True)
    print(f"[embed] Generated {len(embeddings)} embeddings")

    for i, emb in enumerate(embeddings):
        docs[i]["embedding"] = emb

    # Bulk import
    print(f"[db] Importing {len(docs)} documents ...")
    results = client.collections[COLLECTION_NAME].documents.import_(docs, {"action": "upsert"})

    success = sum(1 for r in results if r.get("success", True))
    failed = len(results) - success
    print(f"[db] Imported {success} documents ({failed} failures)")

    if failed > 0:
        for r in results:
            if not r.get("success", True):
                print(f"  FAIL: {r}")
                break


if __name__ == "__main__":
    records = fetch_hscodes()
    asyncio.run(index_into_typesense(records))
    print("[done] HS codes indexed with embeddings successfully")
