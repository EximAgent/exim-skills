"""Serper.dev Google Search + browser-use verification strategy.

Two-phase approach:
1. Serper API returns ranked search candidates
2. High-confidence results are accepted directly
3. Ambiguous results are verified by a browser-use agent that visits the page
   and compares content against the company's CSV data (name, address, products)

This avoids Google CAPTCHAs/rate limits since Serper handles the search,
and the browser only visits candidate company pages (not Google itself).

Model for browser verification is configurable via BROWSER_MODEL_CONFIG env var.
"""

from __future__ import annotations

import os
import re
import sys

import httpx

from cross_validate import extract_homepage, is_directory_url, validate_url
from normalize import fuzzy_match_name_to_domain

# Add skills root for llm module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

SERPER_API_URL = "https://google.serper.dev/search"

# If a candidate scores above this, accept without browser verification
HIGH_CONFIDENCE_SCORE = 4.0

# Max candidates to verify with browser if no high-confidence result
MAX_BROWSER_VERIFICATIONS = 3

# Config key in models.yaml for browser verification agent
BROWSER_CONFIG = os.environ.get("BROWSER_MODEL_CONFIG", "gpt-4o")


async def get_serper_candidates(query: str, company_name: str) -> list[dict]:
	"""Call Serper API and return scored candidates.

	Args:
		query: Search query string.
		company_name: Original company name for scoring.

	Returns:
		List of dicts sorted by score descending:
		[{url, score, title, snippet, homepage}, ...]
	"""
	api_key = os.environ.get("SERPER_API_KEY")
	if not api_key:
		return []

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
		print(f"[serper] API error: {e}")
		return []

	organic = data.get("organic", [])
	if not organic:
		return []

	candidates: list[dict] = []
	name_lower = company_name.lower()
	name_words = [w for w in name_lower.split() if len(w) > 2]

	for result in organic:
		link = result.get("link", "")
		title = (result.get("title", "") or "").lower()
		snippet = (result.get("snippet", "") or "").lower()

		if is_directory_url(link):
			continue

		score = 0.0
		homepage = extract_homepage(link)

		# Domain match (max +3.0)
		domain_score = fuzzy_match_name_to_domain(company_name, link)
		score += domain_score * 3.0

		# Title contains company name words (max +2.0)
		if name_words:
			title_hits = sum(1 for w in name_words if w in title)
			score += (title_hits / len(name_words)) * 2.0

		# Snippet contains company name words (max +1.0)
		if name_words:
			snippet_hits = sum(1 for w in name_words if w in snippet)
			score += (snippet_hits / len(name_words)) * 1.0

		# Bonus for being a homepage URL (short path)
		if link.rstrip("/") == homepage.rstrip("/"):
			score += 0.5

		candidates.append({
			"url": homepage,
			"score": score,
			"title": result.get("title", ""),
			"snippet": result.get("snippet", ""),
		})

	candidates.sort(key=lambda x: x["score"], reverse=True)
	return candidates


async def verify_with_browser(url: str, company_data: dict) -> bool:
	"""Use browser-use agent to visit a URL and verify it matches the company.

	The agent navigates to the page, reads the content, and compares against
	the company's known data (name, address, products, country) from CSV.

	Args:
		url: Candidate company website URL.
		company_data: Dict with company_name, address, product_desc, country.

	Returns:
		True if the browser agent confirms this is the company's website.
	"""
	try:
		from browser_use import Agent, BrowserSession
		from browser_use.browser.profile import BrowserProfile
	except ImportError:
		print("[browser-verify] browser-use not installed, skipping verification")
		return False

	try:
		from llm import get_langchain_llm
		llm = get_langchain_llm(BROWSER_CONFIG)
	except Exception as e:
		print(f"[browser-verify] Failed to create LLM: {e}")
		return False

	company_name = company_data.get("company_name", "")
	address = company_data.get("address", "")
	product_desc = company_data.get("product_desc", "")
	country = company_data.get("country", "")

	# Build comparison context
	context_lines = [f"Company name: {company_name}"]
	if address:
		context_lines.append(f"Address: {address}")
	if product_desc:
		context_lines.append(f"Products/trade goods: {product_desc[:200]}")
	if country:
		context_lines.append(f"Country: {country}")
	context = "\n".join(context_lines)

	task = f"""Visit this website: {url}

I need you to determine if this is the official website of a specific company from international trade records.

Company information from trade data:
{context}

Steps:
1. Look at the website's homepage content
2. Check the company name — does it match or is it a parent/subsidiary?
3. If there's an About page or Contact page, check the address and location
4. Check if the products or services are consistent with the trade goods described

After reviewing, respond with ONLY one of:
- "YES" if this is likely the company's official website
- "NO" if this is a different company or unrelated website

Follow your answer with a brief one-line reason."""

	try:
		profile = BrowserProfile(headless=True)
		session = BrowserSession(browser_profile=profile)

		agent = Agent(
			task=task,
			llm=llm,
			browser_session=session,
			max_actions_per_step=3,
		)

		result = await agent.run(max_steps=6)
		await session.close()

		if result and result.final_result():
			answer = result.final_result().strip().upper()
			is_match = answer.startswith("YES")
			print(f"[browser-verify] {url}: {'YES' if is_match else 'NO'} — {result.final_result().strip()[:100]}")
			return is_match

	except Exception as e:
		print(f"[browser-verify] Error verifying {url}: {e}")

	return False


async def search_serper_with_verification(
	query: str,
	company_name: str,
	company_data: dict,
) -> str | None:
	"""Search via Serper and verify ambiguous results with browser-use agent.

	Flow:
	1. Get ranked candidates from Serper API
	2. If top result has high confidence score → accept directly (validate_url only)
	3. If moderate score → browser-verify the top candidates
	4. Return first verified URL or None

	Args:
		query: Search query string.
		company_name: Company name for scoring.
		company_data: Full company data dict for browser verification context.

	Returns:
		Verified homepage URL or None.
	"""
	candidates = await get_serper_candidates(query, company_name)
	if not candidates:
		print(f"[serper] No candidates found for: {company_name}")
		return None

	top = candidates[0]
	print(f"[serper] Top candidate: {top['url']} (score={top['score']:.2f})")

	# High confidence — accept with basic validation only
	if top["score"] >= HIGH_CONFIDENCE_SCORE:
		if await validate_url(top["url"], company_name):
			print(f"[serper] High confidence, accepted: {top['url']}")
			return top["url"]

	# Moderate/low confidence — try browser verification on top candidates
	for candidate in candidates[:MAX_BROWSER_VERIFICATIONS]:
		url = candidate["url"]
		print(f"[serper] Verifying candidate: {url} (score={candidate['score']:.2f})")

		# Quick validation first (HTTP check, parked domain check)
		if not await validate_url(url, company_name):
			print(f"[serper] Failed basic validation: {url}")
			continue

		# Browser verification
		if await verify_with_browser(url, company_data):
			print(f"[serper] Browser-verified: {url}")
			return url

	print(f"[serper] No candidates verified for: {company_name}")
	return None
