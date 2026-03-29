"""Pydantic v2 models for extracted company data."""

from __future__ import annotations

import json
from pydantic import BaseModel, ConfigDict, Field


class ContactInfo(BaseModel):
	model_config = ConfigDict(extra="allow")
	emails: list[str] = Field(default_factory=list)
	phones: list[str] = Field(default_factory=list)
	fax: str | None = None
	address: str | None = None


class LeadershipEntry(BaseModel):
	name: str
	title: str | None = None


class CompanyProfile(BaseModel):
	model_config = ConfigDict(extra="allow")
	overview: str | None = None
	products_services: list[str] = Field(default_factory=list)
	contact_info: ContactInfo | None = None
	address_hq: str | None = None
	leadership: list[LeadershipEntry] = Field(default_factory=list)
	certifications: list[str] = Field(default_factory=list)
	industries: list[str] = Field(default_factory=list)
	year_founded: str | None = None
	employee_count: str | None = None
	social_links: dict[str, str] = Field(default_factory=dict)
	raw_extracted: dict = Field(default_factory=dict)

	def is_meaningful(self) -> bool:
		"""Check if we extracted enough data to be useful."""
		return bool(self.overview) or len(self.products_services) > 0 or bool(self.contact_info)

	def merge_json_ld(self, data: dict) -> None:
		"""Merge schema.org JSON-LD data into this profile."""
		if not data:
			return
		self.raw_extracted["json_ld"] = data
		ld_type = data.get("@type", "")

		if isinstance(ld_type, list):
			ld_type = ld_type[0] if ld_type else ""

		if ld_type in ("Organization", "Corporation", "LocalBusiness", "Company"):
			self.overview = self.overview or data.get("description")
			self.address_hq = self.address_hq or _format_address(data.get("address"))
			self.year_founded = self.year_founded or data.get("foundingDate")
			self.employee_count = self.employee_count or str(data.get("numberOfEmployees", "")) or None

			if "contactPoint" in data:
				cp = data["contactPoint"]
				if isinstance(cp, list):
					cp = cp[0]
				if isinstance(cp, dict):
					if not self.contact_info:
						self.contact_info = ContactInfo()
					if cp.get("email"):
						self.contact_info.emails.append(cp["email"])
					if cp.get("telephone"):
						self.contact_info.phones.append(cp["telephone"])
					if cp.get("faxNumber"):
						self.contact_info.fax = cp["faxNumber"]

			if "sameAs" in data:
				same_as = data["sameAs"]
				if isinstance(same_as, str):
					same_as = [same_as]
				for url in same_as:
					if "linkedin.com" in url:
						self.social_links["linkedin"] = url
					elif "twitter.com" in url or "x.com" in url:
						self.social_links["twitter"] = url
					elif "facebook.com" in url:
						self.social_links["facebook"] = url
					elif "instagram.com" in url:
						self.social_links["instagram"] = url

	def merge_og(self, og: dict) -> None:
		"""Merge Open Graph meta tag data."""
		if not og:
			return
		self.raw_extracted["open_graph"] = og
		self.overview = self.overview or og.get("og:description")

	def merge_meta(self, meta: dict) -> None:
		"""Merge standard meta tag data."""
		if not meta:
			return
		self.raw_extracted["meta"] = meta
		self.overview = self.overview or meta.get("description")
		if meta.get("keywords"):
			keywords = [k.strip() for k in meta["keywords"].split(",")]
			self.industries.extend(keywords[:10])

	def to_db_row(self) -> dict:
		"""Convert to a dict suitable for Typesense document update."""
		return {
			"overview": self.overview,
			"products_services": json.dumps(self.products_services),
			"contact_info": json.dumps(self.contact_info.model_dump()) if self.contact_info else None,
			"address_hq": self.address_hq,
			"leadership": json.dumps([l.model_dump() for l in self.leadership]),
			"certifications": json.dumps(self.certifications),
			"industries": json.dumps(self.industries),
			"year_founded": self.year_founded,
			"employee_count": self.employee_count,
			"social_links": json.dumps(self.social_links),
			"raw_extracted": json.dumps(self.raw_extracted),
		}

	def to_typesense_doc(self) -> dict:
		"""Convert to a flat dict matching the Typesense collection schema."""
		return {
			"overview": self.overview or "",
			"products_services": self.products_services,
			"contact_info": json.dumps(self.contact_info.model_dump()) if self.contact_info else "",
			"address_hq": self.address_hq or "",
			"leadership": json.dumps([l.model_dump() for l in self.leadership]),
			"certifications": self.certifications,
			"industries": self.industries,
			"year_founded": self.year_founded or "",
			"employee_count": self.employee_count or "",
			"social_links": json.dumps(self.social_links),
			"raw_extracted": json.dumps(self.raw_extracted),
		}

	def generate_embedding_text(self) -> str:
		"""Concatenate all searchable fields into a single text for embedding."""
		parts = [
			self.overview or "",
			self.address_hq or "",
			" ".join(self.products_services),
			" ".join(self.certifications),
			" ".join(self.industries),
		]
		if self.contact_info and self.contact_info.address:
			parts.append(self.contact_info.address)
		return " ".join(p for p in parts if p).strip()


def _format_address(addr: dict | str | None) -> str | None:
	"""Format a schema.org address object into a string."""
	if addr is None:
		return None
	if isinstance(addr, str):
		return addr
	if isinstance(addr, dict):
		parts = [
			addr.get("streetAddress", ""),
			addr.get("addressLocality", ""),
			addr.get("addressRegion", ""),
			addr.get("postalCode", ""),
			addr.get("addressCountry", ""),
		]
		return ", ".join(p for p in parts if p)
	return None
