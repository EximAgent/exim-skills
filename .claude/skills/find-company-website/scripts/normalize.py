"""Company name normalization and fuzzy matching utilities."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

# Legal suffixes to strip for matching purposes
LEGAL_SUFFIXES = re.compile(
	r"\b("
	r"LLC|L\.L\.C\.|INC|INCORPORATED|CORP|CORPORATION|LTD|LIMITED|"
	r"CO|COMPANY|ENTERPRISES|HOLDINGS|GROUP|INTL|INTERNATIONAL|"
	r"S\.?A\.?S\.?|S\.?A\.?|C\.?A\.?|C\.?I\.?|S\.?R\.?L\.?|"
	r"GMBH|AG|PLC|PTY|BV|NV"
	r")\.?\s*$",
	re.IGNORECASE,
)

# Common filler words in addresses
ADDRESS_PARTS = re.compile(
	r"\b(SUITE|STE|FLOOR|FL|ROOM|RM|BLDG|BUILDING|UNIT|APT|"
	r"P\.?O\.?\s*BOX|PO\s*BOX)\b",
	re.IGNORECASE,
)


def normalize_company_name(name: str) -> str:
	"""Normalize a company name for deduplication and matching.

	Strips legal suffixes, extra whitespace, punctuation variations.
	"""
	if not name:
		return ""
	# Uppercase for consistency
	result = name.upper().strip()
	# Remove legal suffixes (may need multiple passes)
	for _ in range(3):
		result = LEGAL_SUFFIXES.sub("", result).strip().rstrip(",").strip()
	# Normalize punctuation
	result = result.replace("&", "AND")
	result = re.sub(r"[^\w\s]", " ", result)
	# Collapse whitespace
	result = re.sub(r"\s+", " ", result).strip()
	return result


def extract_location_from_address(address: str) -> str:
	"""Extract city/state/country from a freeform address string.

	Returns the most useful location fragment for search disambiguation.
	"""
	if not address:
		return ""
	# Try to find city, state pattern (US addresses)
	# e.g., "KANSAS CITY, MO 64151" -> "KANSAS CITY MO"
	match = re.search(
		r"([A-Z][A-Z\s]+),\s*([A-Z]{2})\s*\d{5}",
		address.upper(),
	)
	if match:
		return f"{match.group(1).strip()}, {match.group(2)}"

	# Try country extraction
	match = re.search(
		r"(?:UNITED STATES|USA|US|CHINA|INDIA|COLOMBIA|MEXICO|BRAZIL|"
		r"GERMANY|JAPAN|KOREA|THAILAND|BELGIUM|NETHERLANDS)",
		address.upper(),
	)
	if match:
		return match.group(0)

	# Fallback: take the last meaningful line/segment
	parts = re.split(r"[,\n]", address)
	# Filter out suite/PO box lines
	meaningful = [
		p.strip()
		for p in parts
		if p.strip() and not ADDRESS_PARTS.search(p)
	]
	return meaningful[-1] if meaningful else ""


def fuzzy_match_name_to_domain(company_name: str, url: str) -> float:
	"""Score how well a company name matches a URL's domain.

	Returns a float 0.0-1.0 where higher is better match.
	"""
	if not company_name or not url:
		return 0.0

	normalized = normalize_company_name(company_name).lower()
	words = normalized.split()

	try:
		domain = urlparse(url).netloc.lower()
	except Exception:
		return 0.0

	# Strip www. and TLD
	domain_name = domain.replace("www.", "")
	domain_base = domain_name.split(".")[0] if "." in domain_name else domain_name

	# Check if domain contains significant words from company name
	# (ignoring very short words like "the", "of", "and")
	significant_words = [w for w in words if len(w) > 2]

	if not significant_words:
		return 0.0

	# Score 1: Direct substring match
	matches = sum(1 for w in significant_words if w in domain_base)
	word_score = matches / len(significant_words)

	# Score 2: SequenceMatcher on the concatenated name vs domain
	concat_name = "".join(significant_words)
	seq_score = SequenceMatcher(None, concat_name, domain_base).ratio()

	# Score 3: Check if domain is an acronym of the company name
	acronym = "".join(w[0] for w in significant_words if w)
	acronym_score = 1.0 if acronym == domain_base else 0.0

	return max(word_score, seq_score, acronym_score)


def build_search_query(
	company_name: str,
	address: str = "",
	product_desc: str = "",
	country: str = "",
) -> str:
	"""Build an effective search query from available company data."""
	parts = [company_name]

	location = extract_location_from_address(address)
	if location:
		parts.append(location)

	if product_desc:
		# Truncate long product descriptions
		parts.append(product_desc[:60])

	if country:
		parts.append(country)

	parts.append("official website")
	return " ".join(parts)
