"""Serper.dev Google Search strategy.

Searches Google via Serper API and returns raw results.
The calling agent (Claude Code) reads the URLs, titles, and snippets
to decide which candidates to visit and verify with agent-browser.

Directory/aggregator URLs are filtered out. Everything else is returned
as-is for the agent to reason about.

Supports batch search: send multiple (query, company_name) pairs concurrently
with a semaphore to avoid hammering the API. Serper has no native batch endpoint.
"""

from __future__ import annotations

import asyncio
import os

import httpx
from dotenv import load_dotenv
from tqdm import tqdm

load_dotenv()

from cross_validate import extract_homepage, is_directory_url

SERPER_API_URL = "https://google.serper.dev/search"
BATCH_CONCURRENCY = int(os.environ.get("SERPER_CONCURRENCY", "5"))


async def search_serper(
	query: str,
	company_name: str,
	semaphore: asyncio.Semaphore | None = None,
) -> dict:
	"""Search Google via Serper API and return raw candidates.

	Filters out known directory/aggregator sites but does NOT score or rank.
	The calling agent reads the titles, snippets, and URLs to decide
	which candidates to visit with agent-browser.

	Args:
		query: Search query string.
		company_name: Company name (used only for logging).
		semaphore: Optional semaphore for concurrency control in batch calls.

	Returns:
		Dict with:
		  - candidates: List of {url, title, snippet, position} from Google
		  - query: The search query used
	"""
	async def _call() -> dict:
		api_key = os.environ.get("SERPER_API_KEY")
		if not api_key:
			print("[search] SERPER_API_KEY not set")
			return {"candidates": [], "query": query}

		try:
			async with httpx.AsyncClient(timeout=15.0) as client:
				resp = await client.post(
					SERPER_API_URL,
					headers={
						"X-API-KEY": api_key,
						"Content-Type": "application/json",
					},
					json={"q": query, "num": 10},
				)
				resp.raise_for_status()
				data = resp.json()
		except (httpx.HTTPError, Exception) as e:
			print(f"[search] Serper API error: {e}")
			return {"candidates": [], "query": query}

		organic = data.get("organic", [])
		if not organic:
			return {"candidates": [], "query": query}

		candidates: list[dict] = []
		for i, result in enumerate(organic):
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

	if semaphore:
		async with semaphore:
			return await _call()
	return await _call()


async def search_serper_batch(
	queries: list[tuple[str, str]],
	concurrency: int = BATCH_CONCURRENCY,
) -> list[dict]:
	"""Search Google for multiple (query, company_name) pairs concurrently.

	Serper costs ~$0.001-0.003 per query. Cost shown in progress bar.
	"""
	semaphore = asyncio.Semaphore(concurrency)
	cost_per_query = 0.003  # conservative estimate
	pbar_cost = {"total": 0.0}
	results = [None] * len(queries)
	pbar = tqdm(total=len(queries), desc="search_google")

	async def _run(idx, q, name):
		result = await search_serper(q, name, semaphore)
		results[idx] = result
		pbar_cost["total"] += cost_per_query
		pbar.set_postfix(cost=f"${pbar_cost['total']:.4f}")
		pbar.update(1)

	await asyncio.gather(*[_run(i, q, n) for i, (q, n) in enumerate(queries)])
	pbar.close()
	return results


if __name__ == "__main__":
	import json
	import sys

	if len(sys.argv) < 3:
		print('Usage: python search_google.py "search query" "company name"')
		sys.exit(1)

	result = asyncio.run(search_serper(sys.argv[1], sys.argv[2]))
	print(json.dumps(result, indent=2))
