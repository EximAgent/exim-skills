#!/usr/bin/env python3
"""Main orchestrator for extracting structured company info from websites.

Usage:
	python extract_info.py "https://example.com" [company_id]

Or as a module:
	from extract_info import extract_company_info
	result = await extract_company_info("https://example.com", company_id="SWIFT_BEEF")
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from db import get_typesense_client, init_schema, sync_search_index, upsert_profile
from schema import CompanyProfile
from strategy_static import extract_static
from strategy_playwright import extract_dynamic


async def extract_company_info(
	url: str,
	company_id: str | None = None,
) -> dict:
	"""Extract structured company info from a website URL.

	Tries static HTTP extraction first (fast, cheap), falls back to
	Playwright for JS-rendered SPAs.

	Args:
		url: Company website URL.
		company_id: Optional company_id to store results in Typesense.

	Returns:
		Dict with extracted profile data and metadata.
	"""
	assert url, "URL is required"
	assert url.startswith(("http://", "https://")), f"Invalid URL: {url}"

	result = {
		"url": url,
		"company_id": company_id,
		"extraction_method": None,
		"profile": None,
		"status": "failed",
	}

	# Strategy 1: Static HTTP fetch
	print(f"[extract] Trying static extraction for: {url}")
	profile = await extract_static(url)

	if profile and profile.is_meaningful():
		result["extraction_method"] = "static_html"
		result["profile"] = profile.model_dump()
		result["status"] = "success"
		print(f"[extract] Static extraction succeeded for: {url}")
	else:
		# Strategy 2: Playwright dynamic crawl
		print(f"[extract] Static failed, trying Playwright for: {url}")
		profile = await extract_dynamic(url)

		if profile and profile.is_meaningful():
			result["extraction_method"] = "playwright"
			result["profile"] = profile.model_dump()
			result["status"] = "success"
			print(f"[extract] Playwright extraction succeeded for: {url}")
		else:
			print(f"[extract] All strategies failed for: {url}")
			return result

	# Store in Typesense if company_id provided
	if company_id is not None and profile is not None:
		try:
			client = get_typesense_client()
			init_schema(client)
			upsert_profile(client, company_id, profile.to_db_row(), result["extraction_method"])
			await sync_search_index(client, company_id)
			result["db_stored"] = True
			print(f"[extract] Stored profile in Typesense for company_id={company_id}")
		except Exception as e:
			result["db_error"] = str(e)
			print(f"[extract] Typesense error: {e}")

	return result


async def batch_extract(
	companies: list[dict],
	concurrency: int = 3,
) -> list[dict]:
	"""Extract info from multiple company websites with concurrency control.

	Args:
		companies: List of dicts with 'url' and optional 'company_id'.
		concurrency: Max concurrent extractions.

	Returns:
		List of extraction results.
	"""
	semaphore = asyncio.Semaphore(concurrency)

	async def _extract_one(company: dict) -> dict:
		async with semaphore:
			return await extract_company_info(
				url=company["url"],
				company_id=company.get("company_id"),
			)

	tasks = [_extract_one(c) for c in companies]
	results = await asyncio.gather(*tasks, return_exceptions=True)

	return [
		r if isinstance(r, dict) else {"error": str(r)}
		for r in results
	]


if __name__ == "__main__":
	if len(sys.argv) < 2:
		print("Usage: python extract_info.py <url> [company_id]")
		print("Example: python extract_info.py https://www.swiftbeef.com SWIFT_BEEF")
		sys.exit(1)

	url = sys.argv[1]
	company_id = sys.argv[2] if len(sys.argv) > 2 else None

	result = asyncio.run(extract_company_info(url, company_id))
	print(json.dumps(result, indent=2, default=str))
