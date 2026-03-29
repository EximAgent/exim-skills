"""Playwright dynamic crawl strategy for JS-rendered websites.

Falls back to this when static HTTP extraction fails (SPA, CAPTCHA, etc.).
Uses headless Chromium to render JavaScript and extract the rendered DOM.
"""

from __future__ import annotations

import re

from schema import CompanyProfile, ContactInfo, LeadershipEntry

# Lazy import — playwright may not be installed
_playwright_available: bool | None = None


def _check_playwright() -> bool:
	global _playwright_available
	if _playwright_available is None:
		try:
			import playwright.async_api  # noqa: F401
			_playwright_available = True
		except ImportError:
			_playwright_available = False
	return _playwright_available


async def extract_dynamic(url: str) -> CompanyProfile | None:
	"""Extract company info using Playwright for JS-rendered pages.

	Launches headless Chromium, navigates to the URL, waits for JS to render,
	then extracts data from the rendered DOM.

	Args:
		url: The company website URL to extract from.

	Returns:
		CompanyProfile if meaningful data was extracted, None otherwise.
	"""
	if not _check_playwright():
		print("[playwright] playwright not installed, skipping dynamic extraction")
		return None

	from playwright.async_api import async_playwright

	try:
		async with async_playwright() as p:
			browser = await p.chromium.launch(headless=True)
			context = await browser.new_context(
				user_agent=(
					"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
					"AppleWebKit/537.36 (KHTML, like Gecko) "
					"Chrome/120.0.0.0 Safari/537.36"
				),
				viewport={"width": 1280, "height": 720},
			)
			page = await context.new_page()

			profile = CompanyProfile()

			# Navigate to main page
			await page.goto(url, wait_until="networkidle", timeout=30000)
			await _extract_from_page(page, profile, url, is_homepage=True)

			# Find and crawl key subpages
			subpage_urls = await _find_subpages(page, url)
			for sub_url in subpage_urls[:5]:
				try:
					await page.goto(sub_url, wait_until="networkidle", timeout=20000)
					await _extract_from_page(page, profile, sub_url, is_homepage=False)
				except Exception as e:
					print(f"[playwright] Error on subpage {sub_url}: {e}")

			await browser.close()
			return profile if profile.is_meaningful() else None

	except Exception as e:
		print(f"[playwright] Error during dynamic extraction: {e}")
		return None


async def _extract_from_page(
	page,
	profile: CompanyProfile,
	url: str,
	is_homepage: bool,
) -> None:
	"""Extract data from a rendered Playwright page."""
	from parsers import (
		extract_certifications,
		extract_contact_info,
		extract_json_ld,
		extract_leadership_from_page,
		extract_meta_tags,
		extract_open_graph,
		extract_products_from_page,
	)
	from bs4 import BeautifulSoup

	html = await page.content()
	soup = BeautifulSoup(html, "html.parser")
	text = await page.evaluate("() => document.body.innerText")

	if is_homepage:
		# JSON-LD
		json_ld = extract_json_ld(soup)
		if json_ld:
			profile.merge_json_ld(json_ld)

		# Open Graph + meta tags
		profile.merge_og(extract_open_graph(soup))
		profile.merge_meta(extract_meta_tags(soup))

		# Overview
		if not profile.overview:
			# Try page title + first paragraphs
			title = await page.title()
			meta_desc = soup.find("meta", attrs={"name": "description"})
			if meta_desc and meta_desc.get("content"):
				profile.overview = meta_desc["content"]
			elif text and len(text) > 100:
				# Take first meaningful chunk
				lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 30]
				profile.overview = " ".join(lines[:3])[:2000]

	url_lower = url.lower()

	# Contact info
	contact_data = extract_contact_info(text or "")
	if contact_data.get("emails") or contact_data.get("phones"):
		if not profile.contact_info:
			profile.contact_info = ContactInfo(**contact_data)
		else:
			profile.contact_info.emails.extend(contact_data.get("emails", []))
			profile.contact_info.phones.extend(contact_data.get("phones", []))

	# Products/services page
	if any(kw in url_lower for kw in ("product", "service", "solution")):
		products = extract_products_from_page(soup)
		profile.products_services.extend(products)

	# Team/leadership page
	if any(kw in url_lower for kw in ("team", "leader", "management", "people")):
		leaders = extract_leadership_from_page(soup)
		profile.leadership.extend(LeadershipEntry(**l) for l in leaders)

	# About page
	if any(kw in url_lower for kw in ("about", "company")) and not profile.overview:
		if text and len(text) > 50:
			lines = [l.strip() for l in text.split("\n") if len(l.strip()) > 30]
			profile.overview = " ".join(lines[:5])[:2000]

		year_match = re.search(r"(?:founded|established|since)\s*(?:in\s*)?(\d{4})", text or "", re.IGNORECASE)
		if year_match and not profile.year_founded:
			profile.year_founded = year_match.group(1)

	# Certifications (any page)
	certs = extract_certifications(text or "")
	profile.certifications.extend(certs)


async def _find_subpages(page, base_url: str) -> list[str]:
	"""Find links to key subpages from the current page."""
	from urllib.parse import urlparse, urljoin

	base_domain = urlparse(base_url).netloc
	keywords = ["about", "contact", "products", "services", "team", "leadership"]

	links = await page.evaluate("""() => {
		return Array.from(document.querySelectorAll('a[href]')).map(a => ({
			href: a.href,
			text: a.innerText.trim().toLowerCase()
		}));
	}""")

	found: list[str] = []
	for link in links:
		href = link.get("href", "")
		text = link.get("text", "")
		try:
			parsed = urlparse(href)
			if parsed.netloc and parsed.netloc != base_domain:
				continue
		except Exception:
			continue

		for kw in keywords:
			if kw in href.lower() or kw in text:
				abs_url = urljoin(base_url, href)
				if abs_url not in found:
					found.append(abs_url)
				break

	return found[:8]
