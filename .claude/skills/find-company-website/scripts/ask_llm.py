#!/usr/bin/env python3
"""Ask a foundation model if it knows this company's website.

Returns the raw LLM response. The orchestrator agent reads it and decides
whether to trust it or proceed to search + agent-browser.

Usage:
    python ask_llm.py '[{"company_name": "SWIFT BEEF COMPANY"}, {"company_name": "TYSON FOODS"}]'
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from tqdm import tqdm

# Resolve project root (where llm.py and models.yaml live)
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _PROJECT_ROOT)

from llm import create_agent_from_config

KNOWLEDGE_CONFIG = os.environ.get("KNOWLEDGE_MODEL_CONFIG")
BATCH_CONCURRENCY = int(os.environ.get("ASK_LLM_CONCURRENCY", "10"))


async def ask_llm(company_data: dict, semaphore: asyncio.Semaphore | None = None, pbar_cost: dict | None = None) -> dict:
	"""Ask an LLM if it knows this company's official website.

	Returns:
		Dict with input, raw LLM response, and cost.
	"""
	name = company_data.get("company_name", "")
	assert name, "company_name is required"

	context_parts = [f"Company name: {name}"]
	if company_data.get("address"):
		context_parts.append(f"Address: {company_data['address']}")
	if company_data.get("product_desc"):
		context_parts.append(f"Products/trade: {company_data['product_desc']}")
	if company_data.get("country"):
		context_parts.append(f"Country: {company_data['country']}")

	context = "\n".join(context_parts)

	prompt = f"""Given the following company information from international trade records, identify the official website URL of this company based on your knowledge.

{context}

Respond in JSON format with exactly these fields:
{{
  "url": "https://example.com" or null if unknown,
  "confidence": 0 or 1 or 2,
  "reason": "<brief explanation>"
}}

Confidence levels:
- 0: I don't know this company or its website
- 1: I have a guess but I'm not sure
- 2: I'm confident this is the correct official website

Rules:
- Return the homepage URL only (e.g., https://example.com, not a subpage)
- Do not return social media profiles, directory listings, or aggregator sites
- The company may be known by a parent company name or subsidiary
- Be honest — if you're not sure, use confidence 0 or 1

Respond with ONLY the JSON object, no other text."""

	async def _call() -> dict:
		try:
			agent = create_agent_from_config(KNOWLEDGE_CONFIG)
			response = await agent.async_completions([{"role": "user", "content": prompt}])
			raw_text = (response.content or "").strip()
			cost = response.token_usage.cost if response.token_usage else 0.0
			tokens = response.token_usage.total_tokens if response.token_usage else 0
		except Exception as e:
			return {"input": company_data, "raw": None, "error": str(e), "cost": 0.0, "tokens": 0}

		if pbar_cost is not None:
			pbar_cost["total"] += cost

		return {"input": company_data, "raw": raw_text, "cost": cost, "tokens": tokens}

	if semaphore:
		async with semaphore:
			return await _call()
	return await _call()


async def ask_llm_batch(companies: list[dict], concurrency: int = BATCH_CONCURRENCY) -> list[dict]:
	"""Ask LLM about multiple companies concurrently with cost tracking.

	Returns list of results in the same order as input.
	"""
	semaphore = asyncio.Semaphore(concurrency)
	pbar_cost = {"total": 0.0}
	results = [None] * len(companies)
	pbar = tqdm(total=len(companies), desc="ask_llm")

	async def _run(idx, c):
		result = await ask_llm(c, semaphore, pbar_cost)
		results[idx] = result
		pbar.set_postfix(cost=f"${pbar_cost['total']:.4f}")
		pbar.update(1)

	await asyncio.gather(*[_run(i, c) for i, c in enumerate(companies)])
	pbar.close()
	return results


if __name__ == "__main__":
	if len(sys.argv) < 2:
		print("Usage: python ask_llm.py '[{\"company_name\": \"SWIFT BEEF COMPANY\"}, ...]'")
		sys.exit(1)

	data = json.loads(sys.argv[1])
	if not isinstance(data, list):
		data = [data]
	results = asyncio.run(ask_llm_batch(data))
	print(json.dumps(results, indent=2))
