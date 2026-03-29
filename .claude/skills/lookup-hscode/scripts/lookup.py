"""Look up HS codes by code or keyword.

Supports three search modes:
  - simple (default): Fast lexical search with Typesense typo tolerance. No API calls.
  - advanced:         LLM query expansion + semantic vector search. Uses Gemini API.

Usage:
    python lookup.py "bovine meat"              # simple keyword search (default)
    python lookup.py "0102"                     # code search
    python lookup.py "beef" --mode advanced     # LLM expansion + semantic search
    python lookup.py "01" --level chapter       # filter by level
"""

import asyncio
import json
import os
import re
import sys

import httpx
import typesense

# Add skills root for shared modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "skills"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "skills", "extract-company-info", "scripts"))

from embeddings import generate_query_embedding
from llm import create_agent_from_config

COLLECTION_NAME = os.environ.get("TYPESENSE_HSCODES_COLLECTION", "hscodes")
LLM_CONFIG = os.environ.get("GEMINI_MODEL_CONFIG", "gemini-2.5-flash")

EXPAND_PROMPT = """You are an HS (Harmonized System) code expert. Given a user query about a product or commodity, expand it into the formal HS nomenclature terms that would appear in the official HS classification system.

Rules:
- Output ONLY the expanded search terms, nothing else
- Include the original term plus formal HS equivalents
- Use terms that appear in HS code descriptions (e.g. "bovine" not just "cow", "swine" not just "pig")
- Include related terms at different processing levels (live, fresh, frozen, chilled, preserved, etc.)
- Keep it concise — max 15-20 words total
- Do NOT include generic words like "bone", "shell", "sheet" that match unrelated categories

Examples:
- "beef" → "beef bovine meat fresh frozen chilled boneless carcasses edible offal"
- "pork" → "pork swine meat fresh frozen chilled ham carcasses sausages"
- "chicken" → "chicken poultry fowl gallus domesticus meat fresh frozen chilled"
- "rice" → "rice paddy husked milled broken basmati"
- "steel" → "steel iron ferrous flat-rolled bars rods wire tubes pipes stainless alloy"
- "cotton" → "cotton raw carded combed yarn woven fabrics textile fibres"

User query: {query}"""


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


def _is_code_query(query: str) -> bool:
    """Check if the query looks like an HS code (digits, possibly with dots)."""
    return bool(re.match(r'^[\d.]+$', query.strip()))


async def expand_query(query: str) -> str:
    """Use LLM to expand a product query into HS terminology."""
    agent = create_agent_from_config(LLM_CONFIG)
    prompt = EXPAND_PROMPT.format(query=query)
    response = await agent.async_completions([{"role": "user", "content": prompt}])
    expanded = response.content.strip() if response.content else query
    print(f"[expand] '{query}' → '{expanded}'")
    return expanded


def _extract_hits(results: dict) -> list[tuple[str, dict]]:
    """Extract (code, doc) pairs from Typesense results."""
    hits = []
    for hit in results.get("hits", []):
        doc = hit["document"]
        hits.append((doc["code"], {
            "code": doc["code"],
            "name": doc["name"],
            "level": doc["level"],
            "parent_code": doc.get("parent_code", ""),
            "full_path": doc["full_path"],
        }))
    return hits


async def search_hscodes(
    query: str,
    limit: int = 10,
    level: str = "",
    mode: str = "simple",
) -> list[dict]:
    """Search HS codes by code or keyword.

    Modes:
        simple:   Fast lexical search with Typesense typo tolerance. No API calls.
                  Best for queries already using HS terminology (e.g. "bovine meat").
        advanced: LLM query expansion + semantic vector search. Uses Gemini API.
                  Best for common/informal terms (e.g. "beef", "chicken", "steel pipes").

    Args:
        query: HS code (e.g. "0102") or keyword (e.g. "beef").
        limit: Max results to return.
        level: Filter by level: section, chapter, heading, subheading.
        mode: "simple" (default) or "advanced".

    Returns:
        List of matching HS code records.
    """
    client = get_typesense_client()
    is_code = _is_code_query(query)

    # Code queries always use simple lexical search
    if is_code:
        mode = "simple"

    if mode == "simple":
        params = {
            "q": query.strip(),
            "query_by": "code,name,full_path",
            "per_page": limit,
            "num_typos": 2,
            "highlight_full_fields": "name,full_path",
        }
        if level:
            params["filter_by"] = f"level:={level}"
        results = client.collections[COLLECTION_NAME].documents.search(params)
        return [doc for _, doc in _extract_hits(results)]

    # Advanced mode: LLM expansion + semantic search
    search_query = await expand_query(query)
    query_embedding = await generate_query_embedding(query)

    lexical_params = {
        "q": search_query,
        "query_by": "code,name,full_path",
        "per_page": limit * 2,
        "num_typos": 2,
    }
    if level:
        lexical_params["filter_by"] = f"level:={level}"

    vec_str = ",".join(f"{v:.6f}" for v in query_embedding)
    semantic_params = {
        "q": "*",
        "vector_query": f"embedding:([{vec_str}], k:{limit * 2})",
        "per_page": limit * 2,
    }
    if level:
        semantic_params["filter_by"] = f"level:={level}"

    multi_results = client.multi_search.perform(
        {"searches": [
            {"collection": COLLECTION_NAME, **lexical_params},
            {"collection": COLLECTION_NAME, **semantic_params},
        ]}, {}
    )

    lexical_hits = _extract_hits(multi_results["results"][0])
    semantic_hits = _extract_hits(multi_results["results"][1])

    # Merge: items in both lists rank first, then semantic-only, then lexical-only
    lexical_codes = {code for code, _ in lexical_hits}
    semantic_codes = {code for code, _ in semantic_hits}
    both_codes = lexical_codes & semantic_codes

    merged = []
    seen = set()

    for code, doc in semantic_hits:
        if code in both_codes and code not in seen:
            seen.add(code)
            merged.append(doc)

    for code, doc in semantic_hits:
        if code not in seen:
            seen.add(code)
            merged.append(doc)

    for code, doc in lexical_hits:
        if code not in seen:
            seen.add(code)
            merged.append(doc)

    return merged[:limit]


def format_results(hits: list[dict]) -> str:
    """Format search results for display."""
    if not hits:
        return "No HS codes found."

    lines = []
    for h in hits:
        lines.append(f"  [{h['level']:>10}]  {h['code']:<8}  {h['name']}")
        if h["full_path"] != h["name"]:
            lines.append(f"              path: {h['full_path']}")
    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python lookup.py <query> [--mode simple|advanced] [--level section|chapter|heading|subheading] [--limit N]")
        sys.exit(1)

    query = sys.argv[1]
    level = ""
    limit = 10
    mode = "simple"

    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--level" and i + 1 < len(args):
            level = args[i + 1]
            i += 2
        elif args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1]
            i += 2
        else:
            i += 1

    hits = asyncio.run(search_hscodes(query, limit=limit, level=level, mode=mode))
    print(f"\nFound {len(hits)} results for '{query}' (mode={mode}):\n")
    print(format_results(hits))
