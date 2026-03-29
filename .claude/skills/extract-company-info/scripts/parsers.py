"""HTML parsing utilities for extracting structured company data."""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

# Regex patterns for contact info extraction
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_PATTERN = re.compile(
	r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}"  # US format
	r"|(?:\+\d{1,3}[-.\s]?)?\d{2,4}[-.\s]?\d{3,4}[-.\s]?\d{3,4}"  # International
)
FAX_PATTERN = re.compile(r"(?:fax|facsimile)[:\s]*([+\d\-.\s()]{7,20})", re.IGNORECASE)

# Subpage paths to crawl for company info
SUBPAGE_KEYWORDS = {
	"about": ["about", "about-us", "about_us", "company", "who-we-are", "quienes-somos", "sobre-nosotros"],
	"contact": ["contact", "contact-us", "contact_us", "contacto", "get-in-touch"],
	"products": ["products", "services", "solutions", "productos", "servicios", "what-we-do"],
	"team": ["team", "leadership", "management", "our-team", "equipo", "people"],
}


def extract_json_ld(soup: BeautifulSoup) -> dict | None:
	"""Extract JSON-LD structured data (schema.org) from the page.

	Looks for Organization, Corporation, LocalBusiness types.
	"""
	scripts = soup.find_all("script", type="application/ld+json")
	for script in scripts:
		try:
			data = json.loads(script.string or "")
		except (json.JSONDecodeError, TypeError):
			continue

		# Handle @graph arrays
		if isinstance(data, dict) and "@graph" in data:
			for item in data["@graph"]:
				if _is_org_type(item):
					return item
		elif isinstance(data, list):
			for item in data:
				if isinstance(item, dict) and _is_org_type(item):
					return item
		elif isinstance(data, dict) and _is_org_type(data):
			return data

	return None


def _is_org_type(data: dict) -> bool:
	"""Check if JSON-LD data represents an organization-type entity."""
	ld_type = data.get("@type", "")
	if isinstance(ld_type, list):
		org_types = {"Organization", "Corporation", "LocalBusiness", "Company", "GovernmentOrganization"}
		return bool(set(ld_type) & org_types)
	return ld_type in ("Organization", "Corporation", "LocalBusiness", "Company", "GovernmentOrganization")


def extract_open_graph(soup: BeautifulSoup) -> dict:
	"""Extract Open Graph meta tags."""
	og = {}
	for meta in soup.find_all("meta", property=re.compile(r"^og:")):
		prop = meta.get("property", "")
		content = meta.get("content", "")
		if prop and content:
			og[prop] = content
	return og


def extract_meta_tags(soup: BeautifulSoup) -> dict:
	"""Extract standard meta tags (description, keywords, author)."""
	meta = {}
	for tag in soup.find_all("meta"):
		name = tag.get("name", "").lower()
		content = tag.get("content", "")
		if name in ("description", "keywords", "author") and content:
			meta[name] = content
	return meta


def extract_contact_info(text: str) -> dict:
	"""Extract contact information from page text using regex patterns."""
	emails = list(set(EMAIL_PATTERN.findall(text)))
	# Filter out common non-contact emails
	emails = [
		e for e in emails
		if not any(
			x in e.lower()
			for x in ["example.com", "sentry.io", "wixpress", "schema.org", "w3.org"]
		)
	]

	phones = list(set(PHONE_PATTERN.findall(text)))[:5]

	fax_match = FAX_PATTERN.search(text)
	fax = fax_match.group(1).strip() if fax_match else None

	return {
		"emails": emails[:10],
		"phones": phones,
		"fax": fax,
	}


def extract_about_text(soup: BeautifulSoup) -> str | None:
	"""Extract the main descriptive text about the company.

	Looks for content in <main>, <article>, or substantial <p> tags.
	"""
	# Try <main> or <article> first
	for container_tag in ("main", "article"):
		container = soup.find(container_tag)
		if container:
			paragraphs = container.find_all("p")
			text = " ".join(p.get_text(strip=True) for p in paragraphs[:5])
			if len(text) > 50:
				return text[:2000]

	# Try meta description
	meta_desc = soup.find("meta", attrs={"name": "description"})
	if meta_desc and meta_desc.get("content"):
		return meta_desc["content"]

	# Fallback: find the first substantial paragraph
	for p in soup.find_all("p"):
		text = p.get_text(strip=True)
		if len(text) > 80:
			return text[:2000]

	return None


def extract_nav_links(soup: BeautifulSoup, base_url: str) -> list[str]:
	"""Identify links to key subpages (about, contact, products, team).

	Returns a list of absolute URLs to crawl.
	"""
	found_urls: list[str] = []
	all_links = soup.find_all("a", href=True)

	for keyword_group, patterns in SUBPAGE_KEYWORDS.items():
		for link in all_links:
			href = link["href"].lower().strip()
			link_text = link.get_text(strip=True).lower()

			for pattern in patterns:
				if pattern in href or pattern in link_text:
					abs_url = urljoin(base_url, link["href"])
					# Only follow same-domain links
					if urlparse(abs_url).netloc == urlparse(base_url).netloc:
						if abs_url not in found_urls:
							found_urls.append(abs_url)
					break

	return found_urls[:8]


def extract_products_from_page(soup: BeautifulSoup) -> list[str]:
	"""Extract product/service names from a products or services page."""
	products: list[str] = []

	# Look for product headings
	for heading in soup.find_all(["h2", "h3", "h4"]):
		text = heading.get_text(strip=True)
		if 5 < len(text) < 100:
			products.append(text)

	# Look for product list items
	for li in soup.find_all("li"):
		parent = li.parent
		if parent and parent.name in ("ul", "ol"):
			text = li.get_text(strip=True)
			if 5 < len(text) < 150 and text not in products:
				products.append(text)

	return products[:30]


def extract_leadership_from_page(soup: BeautifulSoup) -> list[dict]:
	"""Extract leadership/team member names and titles."""
	leaders: list[dict] = []

	# Look for common patterns: name in heading, title in subtitle/paragraph
	for card in soup.find_all(["div", "li", "article"], class_=re.compile(
		r"team|member|person|leader|executive|staff", re.IGNORECASE
	)):
		name_el = card.find(["h2", "h3", "h4", "strong"])
		title_el = card.find(["p", "span", "small"])
		if name_el:
			name = name_el.get_text(strip=True)
			title = title_el.get_text(strip=True) if title_el else None
			if 2 < len(name) < 80:
				leaders.append({"name": name, "title": title})

	return leaders[:20]


def extract_certifications(text: str) -> list[str]:
	"""Extract certification mentions from page text."""
	cert_patterns = [
		r"ISO\s*\d{4,5}(?::\d{4})?",
		r"(?:FDA|USDA|CE|GMP|HACCP|OHSAS|SA\s*8000)\s*(?:certified|approved|compliant)?",
		r"(?:Fair\s*Trade|Organic|B\s*Corp|LEED)\s*(?:certified|certification)?",
	]
	certs: list[str] = []
	for pattern in cert_patterns:
		matches = re.findall(pattern, text, re.IGNORECASE)
		certs.extend(m.strip() for m in matches)
	return list(set(certs))
