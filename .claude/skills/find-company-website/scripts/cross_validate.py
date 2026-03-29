"""Cross-validation of candidate company website URLs."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from normalize import fuzzy_match_name_to_domain

# Domains that are directories/aggregators, not company websites
DIRECTORY_DOMAINS = {
	"linkedin.com", "facebook.com", "twitter.com", "x.com",
	"instagram.com", "youtube.com", "tiktok.com",
	"bloomberg.com", "dnb.com", "importgenius.com",
	"yellowpages.com", "yelp.com", "bbb.org",
	"crunchbase.com", "zoominfo.com", "pitchbook.com",
	"glassdoor.com", "indeed.com", "wikipedia.org",
	"panjiva.com", "importyeti.com", "trademap.org",
	"alibaba.com", "globalsources.com", "thomasnet.com",
	"amazon.com", "ebay.com",
}

# Patterns indicating a parked or for-sale domain
PARKED_PATTERNS = re.compile(
	r"domain\s+(is\s+)?for\s+sale|buy\s+this\s+domain|"
	r"parked\s+(free|domain)|coming\s+soon|under\s+construction|"
	r"this\s+domain\s+has\s+expired|godaddy|namecheap\s+parking",
	re.IGNORECASE,
)


def is_directory_url(url: str) -> bool:
	"""Check if URL belongs to a known directory/aggregator site."""
	try:
		domain = urlparse(url).netloc.lower().replace("www.", "")
	except Exception:
		return True
	return any(domain.endswith(d) for d in DIRECTORY_DOMAINS)


def extract_homepage(url: str) -> str:
	"""Extract the homepage URL from a deeper page URL."""
	try:
		parsed = urlparse(url)
		return f"{parsed.scheme}://{parsed.netloc}"
	except Exception:
		return url


async def validate_url(
	url: str,
	company_name: str,
	timeout: float = 10.0,
) -> bool:
	"""Validate that a URL is a live, relevant company website.

	Checks:
	1. URL is not a known directory site
	2. HTTP response is successful (200/301/302)
	3. Domain has reasonable fuzzy match to company name
	4. Page is not a parked/for-sale domain
	"""
	if is_directory_url(url):
		return False

	# Domain fuzzy match — require at least 0.2 score
	# (low threshold because company names often don't match domains,
	# e.g. "SWIFT BEEF COMPANY" -> "jbsusa.com")
	domain_score = fuzzy_match_name_to_domain(company_name, url)

	try:
		async with httpx.AsyncClient(
			follow_redirects=True, timeout=timeout, verify=False
		) as client:
			resp = await client.get(url, headers={
				"User-Agent": (
					"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
					"AppleWebKit/537.36 (KHTML, like Gecko) "
					"Chrome/120.0.0.0 Safari/537.36"
				),
			})

			if resp.status_code >= 400:
				return False

			# Check for parked domain indicators
			text = resp.text[:5000]
			if PARKED_PATTERNS.search(text):
				return False

			# If domain doesn't match name well, check page content
			if domain_score < 0.3:
				name_upper = company_name.upper()
				# Check if company name appears in page title or body
				name_words = [w for w in name_upper.split() if len(w) > 3]
				text_upper = text.upper()
				word_hits = sum(1 for w in name_words if w in text_upper)
				if name_words and word_hits / len(name_words) < 0.3:
					return False

			return True

	except (httpx.HTTPError, Exception):
		# Network errors — URL might still be valid but temporarily down
		# Accept if domain match is strong enough
		return domain_score >= 0.5
