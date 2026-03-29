#!/usr/bin/env python3
"""Main orchestrator for finding company websites from trade CSV data.

Runs both strategies and returns the full results so the calling agent can
reason about confidence, compare evidence, and decide what to accept.

Output structure:
  {
    "canonical_name": "SWIFT BEEF",
    "gemini": {url, confidence, reason, url_valid},
    "serper": {url, candidates, query},
    "recommendation": {url, method, company_id},
    "status": "found" | "not_found"
  }

Usage:
    python find_website.py '{"company_name": "SWIFT BEEF COMPANY", "address": "GREELEY, CO"}'

Or as a module:
    from find_website import find_company_website
    result = await find_company_website(company_data)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

# Add parent paths so we can import from extract-company-info shared db
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(
	os.path.dirname(__file__), "..", "..", "extract-company-info", "scripts"
))

from cross_validate import validate_url
from normalize import build_search_query, normalize_company_name
from strategy_gemini import search_gemini

# Import domain-based ID helper from extract-company-info
try:
	from db import url_to_company_id
except ImportError:
	from urllib.parse import urlparse

	def url_to_company_id(url: str) -> str:
		parsed = urlparse(url)
		domain = (parsed.netloc or parsed.path).split(":")[0].lower().strip(".")
		return domain[4:] if domain.startswith("www.") else domain


async def find_company_website(company_data: dict) -> dict:
	"""Find a company's official website using available strategies.

	Runs Strategy 1 (Gemini LLM) and optionally Strategy 2 (Serper + Browser).
	Returns the full results from each strategy — no hardcoded confidence thresholds.
	The calling agent decides whether to accept, retry, or try another approach
	based on the confidence scores, reasoning, and candidate list.

	Args:
		company_data: Dict with keys: company_name (required), address,
		              product_desc, country, hs_code (all optional).

	Returns:
		Dict with:
		  - gemini: {url, confidence, reason, url_valid} — LLM lookup result
		  - serper: {url, candidates, query} — search + verification result
		  - recommendation: {url, method, company_id} — best result (agent can override)
		  - canonical_name, status
	"""
	name = company_data.get("company_name", "")
	address = company_data.get("address", "")
	product_desc = company_data.get("product_desc", "")
	country = company_data.get("country", "")

	assert name, "company_name is required"

	canonical = normalize_company_name(name)
	query = build_search_query(name, address, product_desc, country)

	result: dict = {
		"canonical_name": canonical,
		"input": company_data,
		"gemini": None,
		"serper": None,
		"recommendation": None,
		"status": "not_found",
	}

	# --- Strategy 1: Gemini LLM ---
	try:
		print(f"[find-website] Strategy 1: Gemini for '{name}'")
		gemini = await search_gemini(name, address, product_desc, country)

		url_valid = False
		if gemini.url:
			url_valid = await validate_url(gemini.url, name)

		result["gemini"] = {
			"url": gemini.url,
			"confidence": gemini.confidence,
			"reason": gemini.reason,
			"url_valid": url_valid,
		}

		print(f"[find-website] Gemini: url={gemini.url}, confidence={gemini.confidence}/10, "
			  f"valid={url_valid}, reason={gemini.reason}")

	except Exception as e:
		result["gemini"] = {"url": None, "confidence": 0, "reason": f"error: {e}", "url_valid": False}
		print(f"[find-website] Gemini error: {e}")

	# --- Strategy 2: Serper + Browser verification ---
	if os.environ.get("SERPER_API_KEY"):
		try:
			print(f"[find-website] Strategy 2: Serper + Browser for '{name}'")
			from strategy_serper import get_serper_candidates, search_serper_with_verification

			candidates = await get_serper_candidates(query, name)
			verified_url = await search_serper_with_verification(query, name, company_data)

			result["serper"] = {
				"url": verified_url,
				"query": query,
				"candidates": [
					{"url": c["url"], "score": round(c["score"], 2), "title": c["title"]}
					for c in candidates[:5]
				],
			}

			print(f"[find-website] Serper: verified={verified_url}, candidates={len(candidates)}")

		except Exception as e:
			result["serper"] = {"url": None, "query": query, "candidates": [], "error": str(e)}
			print(f"[find-website] Serper error: {e}")

	# --- Build recommendation (agent can override) ---
	gemini_data = result.get("gemini") or {}
	serper_data = result.get("serper") or {}

	if serper_data.get("url"):
		# Serper+Browser verified = strongest evidence
		best_url = serper_data["url"]
		best_method = "serper_browser"
	elif gemini_data.get("url") and gemini_data.get("url_valid"):
		# Gemini result that passed URL validation
		best_url = gemini_data["url"]
		best_method = "gemini"
	else:
		best_url = None
		best_method = None

	if best_url:
		result["recommendation"] = {
			"url": best_url,
			"method": best_method,
			"company_id": url_to_company_id(best_url),
		}
		result["status"] = "found"

	return result


async def process_csv_row(row: dict, source_type: str) -> dict:
	"""Process a single CSV row to find the company website.

	Handles different CSV formats (US Exports, US Imports, Colombian Exports).
	"""
	if source_type == "us_export":
		return await find_company_website({
			"company_name": row.get("US_Exporter", ""),
			"address": row.get("US_Exporter_Address", ""),
			"product_desc": row.get("Product_Detailed_Description", ""),
			"country": row.get("Country_of_Foreign_Port", ""),
			"hs_code": row.get("HS_Code", ""),
		})
	elif source_type == "us_import":
		results = []
		for name_field, addr_field in [
			("Shipper Name", "Shipper Address "),
			("Consignee Name", "Consignee Address "),
		]:
			if row.get(name_field):
				result = await find_company_website({
					"company_name": row[name_field],
					"address": row.get(addr_field, ""),
					"product_desc": row.get("Product Desc", ""),
					"country": row.get("Country", ""),
					"hs_code": row.get("HS Code", ""),
				})
				results.append(result)
		return results[0] if results else {"status": "no_company_name"}
	elif source_type == "co_export":
		return await find_company_website({
			"company_name": row.get("RAZON_SOCIAL_EXPORTADOR", ""),
			"address": row.get("DIREC_EXPORTADOR", ""),
			"product_desc": row.get("SUBPARTIDA", ""),
			"country": row.get("PAIS_DESTINO_FINAL", ""),
			"hs_code": row.get("SUBPARTIDA", ""),
		})
	else:
		raise ValueError(f"Unknown source_type: {source_type}")


if __name__ == "__main__":
	if len(sys.argv) < 2:
		print("Usage: python find_website.py '<json_company_data>'")
		print('Example: python find_website.py \'{"company_name": "SWIFT BEEF COMPANY"}\'')
		sys.exit(1)

	data = json.loads(sys.argv[1])
	result = asyncio.run(find_company_website(data))
	print(json.dumps(result, indent=2))
