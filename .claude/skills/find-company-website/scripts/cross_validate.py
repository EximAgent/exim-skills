"""Filters for search results — directory detection and URL extraction."""

from __future__ import annotations

from urllib.parse import urlparse

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
