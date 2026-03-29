"""LLM strategy for finding company websites with confidence scoring.

Uses the unified LLM provider abstraction from llm.py (adapted from simple-llm-api).
Model is configurable via GEMINI_MODEL_CONFIG env var — references a key in models.yaml.

Returns a structured result with URL, confidence score (1-10), and reasoning.
If confidence < MIN_CONFIDENCE (default 5), the caller should fall back to Strategy 2.

To switch providers, just change the config key:
    GEMINI_MODEL_CONFIG=gpt-4o           # use OpenAI instead
    GEMINI_MODEL_CONFIG=claude-sonnet-4  # use Anthropic instead
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

# Add skills root so we can import the shared llm module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from llm import create_agent_from_config

# Config key in models.yaml — override via env var
GEMINI_CONFIG = os.environ.get("GEMINI_MODEL_CONFIG", "gemini-2.5-flash")



@dataclass
class GeminiResult:
	"""Structured result from Gemini website lookup."""
	url: str | None
	confidence: int  # 1-10
	reason: str


async def search_gemini(
	company_name: str,
	address: str = "",
	product_desc: str = "",
	country: str = "",
) -> GeminiResult:
	"""Ask an LLM to identify a company's official website with confidence scoring.

	The model returns a structured JSON response with:
	- url: the homepage URL or null
	- confidence: 1-10 score indicating how sure it is
	- reason: brief explanation of why it chose this URL or why it's unsure

	Args:
		company_name: Company name from trade data.
		address: Company address from trade data.
		product_desc: Product description / HS code context.
		country: Country of origin/destination.

	Returns:
		GeminiResult with url, confidence (1-10), and reason.
	"""
	# Build context from available data
	context_parts = [f"Company name: {company_name}"]
	if address:
		context_parts.append(f"Address: {address}")
	if product_desc:
		context_parts.append(f"Products/trade: {product_desc[:200]}")
	if country:
		context_parts.append(f"Country: {country}")

	context = "\n".join(context_parts)

	prompt = f"""Given the following company information from international trade records, identify the official website URL of this company.

{context}

Respond in JSON format with exactly these fields:
{{
  "url": "https://example.com" or null if unknown,
  "confidence": <integer 1-10>,
  "reason": "<brief explanation>"
}}

Confidence scale:
- 9-10: Certain — well-known company, URL is definitive
- 7-8: High — strong match, company name clearly maps to this domain
- 5-6: Moderate — likely correct but could be a different entity with similar name
- 3-4: Low — guessing based on partial name match or limited info
- 1-2: Very low — mostly a guess, very little supporting evidence

Rules:
- Return the homepage URL only (e.g., https://example.com, not a subpage)
- Do not return social media profiles, directory listings, or aggregator sites
- The company may be known by a parent company name or subsidiary
- If you cannot identify the website with any confidence, set url to null and confidence to 1
- In the reason, explain what evidence supports or undermines your identification

Respond with ONLY the JSON object, no other text."""

	try:
		agent = create_agent_from_config(GEMINI_CONFIG)
		response = await agent.async_completions([{"role": "user", "content": prompt}])
		text = (response.content or "").strip()
	except Exception as e:
		print(f"[gemini] API error: {e}")
		return GeminiResult(url=None, confidence=0, reason=f"API error: {e}")

	if not text:
		return GeminiResult(url=None, confidence=0, reason="Empty response")

	# Parse JSON response
	try:
		# Strip markdown code fences if present
		clean = text
		if clean.startswith("```"):
			clean = re.sub(r"^```(?:json)?\s*", "", clean)
			clean = re.sub(r"\s*```$", "", clean)

		data = json.loads(clean)

		url = data.get("url")
		confidence = int(data.get("confidence", 0))
		reason = data.get("reason", "")

		# Validate URL format
		if url and not re.match(r"https?://", url):
			url = None
			confidence = min(confidence, 2)
			reason = f"Invalid URL format. Original reason: {reason}"

		# Clamp confidence to 1-10
		confidence = max(0, min(10, confidence))

		return GeminiResult(url=url, confidence=confidence, reason=reason)

	except (json.JSONDecodeError, ValueError, TypeError):
		# Fallback: try to extract URL from unstructured response
		url_match = re.search(r"https?://[^\s\"'<>]+", text)
		if url_match:
			url = url_match.group(0).rstrip(".,;)")
			return GeminiResult(
				url=url,
				confidence=3,
				reason=f"Parsed URL from unstructured response: {text[:100]}",
			)
		return GeminiResult(url=None, confidence=0, reason=f"Failed to parse response: {text[:200]}")
