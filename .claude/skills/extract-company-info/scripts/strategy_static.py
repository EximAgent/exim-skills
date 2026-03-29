"""Static HTML extraction strategy using HTTP + BeautifulSoup.

Fetches raw HTML via HTTP and parses it without JavaScript rendering.
Fast and cheap — works for most traditional websites.
Falls back to Playwright for JS-rendered SPAs.
"""

from __future__ import annotations

import re

import httpx
from bs4 import BeautifulSoup

from parsers import (
	extract_about_text,
	extract_certifications,
	extract_contact_info,
	extract_json_ld,
	extract_leadership_from_page,
	extract_meta_tags,
	extract_nav_links,
	extract_open_graph,
	extract_products_from_page,
)
from schema import CompanyProfile, ContactInfo, LeadershipEntry

USER_AGENT = (
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
	"AppleWebKit/537.36 (KHTML, like Gecko) "
	"Chrome/120.0.0.0 Safari/537.36"
)


async def extract_static(url: str) -> CompanyProfile | None:
	"""Extract company info from a website using static HTTP fetching.

	Args:
		url: The company website URL to extract from.

	Returns:
		CompanyProfile if meaningful data was extracted, None if the page
		appears to be JS-rendered or inaccessible.
	"""
	try:
		async with httpx.AsyncClient(
			follow_redirects=True,
			timeout=20.0,
			verify=False,
		) as client:
			headers = {"User-Agent": USER_AGENT}
			resp = await client.get(url, headers=headers)

			if resp.status_code != 200:
				print(f"[static] HTTP {resp.status_code} for {url}")
				return None

			soup = BeautifulSoup(resp.text, "html.parser")
			text_content = soup.get_text(separator=" ", strip=True)

			# If less than 200 chars of text, likely JS-rendered SPA
			if len(text_content) < 200:
				print(f"[static] Too little text content ({len(text_content)} chars), likely SPA")
				return None

			profile = CompanyProfile()

			# 1. JSON-LD structured data (richest source)
			json_ld = extract_json_ld(soup)
			if json_ld:
				profile.merge_json_ld(json_ld)

			# 2. Open Graph meta tags
			profile.merge_og(extract_open_graph(soup))

			# 3. Standard meta tags
			profile.merge_meta(extract_meta_tags(soup))

			# 4. Contact info from page text
			contact_data = extract_contact_info(text_content)
			if contact_data.get("emails") or contact_data.get("phones"):
				if not profile.contact_info:
					profile.contact_info = ContactInfo(**contact_data)
				else:
					profile.contact_info.emails.extend(contact_data.get("emails", []))
					profile.contact_info.phones.extend(contact_data.get("phones", []))

			# 5. Overview text
			if not profile.overview:
				profile.overview = extract_about_text(soup)

			# 6. Certifications
			certs = extract_certifications(text_content)
			if certs:
				profile.certifications.extend(certs)

			# 7. Crawl key subpages for richer data
			subpages = extract_nav_links(soup, url)
			for subpage_url in subpages[:5]:
				try:
					sub_resp = await client.get(subpage_url, headers=headers)
					if sub_resp.status_code == 200:
						sub_soup = BeautifulSoup(sub_resp.text, "html.parser")
						_enrich_from_subpage(profile, sub_soup, subpage_url)
				except (httpx.HTTPError, Exception) as e:
					print(f"[static] Error fetching subpage {subpage_url}: {e}")
					continue

			return profile

	except (httpx.HTTPError, Exception) as e:
		print(f"[static] Error fetching {url}: {e}")
		return None


def _enrich_from_subpage(
	profile: CompanyProfile,
	soup: BeautifulSoup,
	url: str,
) -> None:
	"""Extract additional data from a subpage based on its URL pattern."""
	url_lower = url.lower()
	text = soup.get_text(separator=" ", strip=True)

	if any(kw in url_lower for kw in ("about", "company", "quien", "sobre")):
		if not profile.overview:
			profile.overview = extract_about_text(soup)
		# Look for founding year, employee count
		year_match = re.search(r"(?:founded|established|since)\s*(?:in\s*)?(\d{4})", text, re.IGNORECASE)
		if year_match and not profile.year_founded:
			profile.year_founded = year_match.group(1)
		emp_match = re.search(r"(\d[\d,]+)\s*(?:employees|team members|associates)", text, re.IGNORECASE)
		if emp_match and not profile.employee_count:
			profile.employee_count = emp_match.group(1)

	elif any(kw in url_lower for kw in ("product", "service", "solution")):
		products = extract_products_from_page(soup)
		if products:
			profile.products_services.extend(products)

	elif any(kw in url_lower for kw in ("team", "leader", "management", "people")):
		leaders = extract_leadership_from_page(soup)
		if leaders:
			profile.leadership.extend(
				LeadershipEntry(**l) for l in leaders
			)

	elif any(kw in url_lower for kw in ("contact",)):
		contact_data = extract_contact_info(text)
		if not profile.contact_info:
			profile.contact_info = ContactInfo(**contact_data)
		else:
			profile.contact_info.emails.extend(contact_data.get("emails", []))
			profile.contact_info.phones.extend(contact_data.get("phones", []))

		# Look for a physical address
		address_match = re.search(
			r"\d+\s+[\w\s]+(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln)"
			r"[^<\n]{5,100}",
			text,
			re.IGNORECASE,
		)
		if address_match and not profile.address_hq:
			profile.address_hq = address_match.group(0).strip()

	# Always check for certifications on any page
	certs = extract_certifications(text)
	if certs:
		profile.certifications.extend(certs)
