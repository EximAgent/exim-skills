"""Typesense database operations for company data.

Provides hybrid search (lexical + semantic) using Typesense with Gemini embeddings.
Replaces the previous SQLite + FTS5 implementation with a denormalized document model
optimized for faceted search and LLM-agent queries.

Requires:
	- TYPESENSE_API_KEY (default: 'xyz' for local dev)
	- TYPESENSE_HOST (default: 'localhost')
	- TYPESENSE_PORT (default: '8108')
	- GEMINI_API_KEY (for semantic embeddings)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import typesense
from typesense.exceptions import ObjectAlreadyExists, ObjectNotFound

from embeddings import EMBEDDING_DIM, generate_embedding

COLLECTION_NAME = os.environ.get("TYPESENSE_COLLECTION", "companies")

# -- Typesense collection schema --

COMPANY_SCHEMA = {
	"name": COLLECTION_NAME,
	"fields": [
		# Identity
		{"name": "company_id", "type": "string"},
		{"name": "canonical_name", "type": "string"},
		{"name": "website_url", "type": "string", "optional": True},
		{"name": "discovery_method", "type": "string", "optional": True, "facet": True},
		{"name": "website_verified_at", "type": "string", "optional": True},

		# Profile (extracted from website)
		{"name": "overview", "type": "string", "optional": True},
		{"name": "address_hq", "type": "string", "optional": True},
		{"name": "year_founded", "type": "string", "optional": True},
		{"name": "employee_count", "type": "string", "optional": True},
		{"name": "extraction_method", "type": "string", "optional": True, "facet": True},

		# Array fields (searchable + facetable)
		{"name": "products_services", "type": "string[]", "optional": True, "facet": True},
		{"name": "certifications", "type": "string[]", "optional": True, "facet": True},
		{"name": "industries", "type": "string[]", "optional": True, "facet": True},
		{"name": "hs_codes", "type": "string[]", "optional": True, "facet": True},
		{"name": "countries", "type": "string[]", "optional": True, "facet": True},
		{"name": "product_descs", "type": "string[]", "optional": True},
		{"name": "trade_roles", "type": "string[]", "optional": True, "facet": True},

		# Structured data stored as JSON strings
		{"name": "contact_info", "type": "string", "optional": True, "index": False},
		{"name": "leadership", "type": "string", "optional": True, "index": False},
		{"name": "social_links", "type": "string", "optional": True, "index": False},
		{"name": "raw_extracted", "type": "string", "optional": True, "index": False},

		# Trade aggregates
		{"name": "total_fob_usd", "type": "float", "optional": True, "facet": True},
		{"name": "total_weight_kg", "type": "float", "optional": True},
		{"name": "trade_count", "type": "int32", "optional": True, "facet": True},

		# Trade records stored as JSON array
		{"name": "trade_records_json", "type": "string", "optional": True, "index": False},

		# Semantic vector (configurable via GEMINI_EMBEDDING_MODEL / EMBEDDING_DIM)
		{"name": "embedding", "type": "float[]", "num_dim": EMBEDDING_DIM, "optional": True},

		# Timestamps
		{"name": "created_at", "type": "int64", "optional": True},
		{"name": "updated_at", "type": "int64"},
	],
	"default_sorting_field": "updated_at",
}


def get_typesense_client() -> typesense.Client:
	"""Create a Typesense client from environment variables."""
	return typesense.Client({
		"api_key": os.environ.get("TYPESENSE_API_KEY", "xyz"),
		"nodes": [{
			"host": os.environ.get("TYPESENSE_HOST", "localhost"),
			"port": os.environ.get("TYPESENSE_PORT", "8108"),
			"protocol": os.environ.get("TYPESENSE_PROTOCOL", "http"),
		}],
		"connection_timeout_seconds": 10,
	})


def init_schema(client: typesense.Client | None = None) -> None:
	"""Create the companies collection if it doesn't exist."""
	if client is None:
		client = get_typesense_client()
	try:
		client.collections.create(COMPANY_SCHEMA)
		print(f"[db] Created collection '{COLLECTION_NAME}'")
	except ObjectAlreadyExists:
		pass


def _now_ts() -> int:
	return int(datetime.now(timezone.utc).timestamp())


def _now_iso() -> str:
	return datetime.now(timezone.utc).isoformat()


def get_or_create_company(
	client: typesense.Client,
	canonical_name: str,
	website_url: str | None = None,
) -> str:
	"""Get existing company_id or create a new company document.

	Uses the domain name from website_url as the document ID (e.g., 'swiftbeef.com').
	Falls back to a slugified canonical_name if no URL is available.
	Returns the company_id (document id).
	"""
	doc_id = url_to_company_id(website_url) if website_url else _name_to_id(canonical_name)

	try:
		client.collections[COLLECTION_NAME].documents[doc_id].retrieve()
		return doc_id
	except ObjectNotFound:
		pass

	doc = {
		"id": doc_id,
		"company_id": doc_id,
		"canonical_name": canonical_name,
		"hs_codes": [],
		"countries": [],
		"product_descs": [],
		"products_services": [],
		"certifications": [],
		"industries": [],
		"trade_roles": [],
		"trade_count": 0,
		"created_at": _now_ts(),
		"updated_at": _now_ts(),
	}
	if website_url:
		doc["website_url"] = website_url
	client.collections[COLLECTION_NAME].documents.create(doc)
	return doc_id


def update_company_website(
	client: typesense.Client,
	company_id: str,
	website_url: str,
	method: str,
) -> None:
	"""Update a company's website URL and discovery method."""
	client.collections[COLLECTION_NAME].documents[company_id].update({
		"website_url": website_url,
		"discovery_method": method,
		"website_verified_at": _now_iso(),
		"updated_at": _now_ts(),
	})


def insert_trade_record(client: typesense.Client, record: dict) -> None:
	"""Append a trade record to a company's denormalized document.

	Aggregates hs_codes, countries, product_descs, and trade stats
	into the company document.
	"""
	company_id = record["company_id"]

	try:
		doc = client.collections[COLLECTION_NAME].documents[company_id].retrieve()
	except ObjectNotFound:
		raise ValueError(f"Company {company_id} not found. Call get_or_create_company first.")

	# Aggregate arrays (deduplicate)
	hs_codes = list(set(doc.get("hs_codes", []) + ([record["hs_code"]] if record.get("hs_code") else [])))
	countries = list(set(doc.get("countries", []) + ([record["country"]] if record.get("country") else [])))
	product_descs = doc.get("product_descs", [])
	if record.get("product_desc") and record["product_desc"] not in product_descs:
		product_descs = product_descs + [record["product_desc"]]
	trade_roles = list(set(doc.get("trade_roles", []) + ([record["role"]] if record.get("role") else [])))

	# Aggregate stats
	trade_count = (doc.get("trade_count") or 0) + 1
	total_fob = (doc.get("total_fob_usd") or 0.0) + (record.get("valor_fob_usd") or 0.0)
	total_weight = (doc.get("total_weight_kg") or 0.0) + (record.get("weight_kg") or 0.0)

	# Append raw trade record to JSON array
	existing_records = json.loads(doc.get("trade_records_json", "[]") or "[]")
	existing_records.append({
		k: v for k, v in record.items()
		if k != "company_id" and v is not None
	})

	update = {
		"hs_codes": hs_codes,
		"countries": countries,
		"product_descs": product_descs[:100],  # cap at 100 unique descriptions
		"trade_roles": trade_roles,
		"trade_count": trade_count,
		"total_fob_usd": total_fob,
		"total_weight_kg": total_weight,
		"trade_records_json": json.dumps(existing_records),
		"updated_at": _now_ts(),
	}

	client.collections[COLLECTION_NAME].documents[company_id].update(update)


def upsert_profile(
	client: typesense.Client,
	company_id: str,
	profile_data: dict,
	method: str,
) -> None:
	"""Update a company document with extracted profile data."""
	update = {
		"overview": profile_data.get("overview") or "",
		"products_services": json.loads(profile_data.get("products_services", "[]") or "[]"),
		"contact_info": profile_data.get("contact_info") or "",
		"address_hq": profile_data.get("address_hq") or "",
		"leadership": profile_data.get("leadership") or "",
		"certifications": json.loads(profile_data.get("certifications", "[]") or "[]"),
		"industries": json.loads(profile_data.get("industries", "[]") or "[]"),
		"year_founded": profile_data.get("year_founded") or "",
		"employee_count": profile_data.get("employee_count") or "",
		"social_links": profile_data.get("social_links") or "",
		"raw_extracted": profile_data.get("raw_extracted") or "",
		"extraction_method": method,
		"updated_at": _now_ts(),
	}

	client.collections[COLLECTION_NAME].documents[company_id].update(update)


async def sync_search_index(client: typesense.Client, company_id: str) -> None:
	"""Generate and store a semantic embedding for the company document.

	Concatenates all searchable text fields and generates a Gemini embedding
	for semantic search.
	"""
	doc = client.collections[COLLECTION_NAME].documents[company_id].retrieve()

	# Build embedding text from all relevant fields
	parts = [
		doc.get("canonical_name", ""),
		doc.get("overview", ""),
		doc.get("address_hq", ""),
		" ".join(doc.get("products_services", [])),
		" ".join(doc.get("product_descs", [])),
		" ".join(doc.get("hs_codes", [])),
		" ".join(doc.get("countries", [])),
		" ".join(doc.get("certifications", [])),
		" ".join(doc.get("industries", [])),
	]
	text = " ".join(p for p in parts if p).strip()

	if not text:
		return

	embedding = await generate_embedding(text)

	client.collections[COLLECTION_NAME].documents[company_id].update({
		"embedding": embedding,
		"updated_at": _now_ts(),
	})


def search_companies(
	client: typesense.Client,
	query: str,
	limit: int = 20,
	filter_by: str = "",
	facet_by: str = "",
) -> dict:
	"""Lexical full-text search across company documents.

	Args:
		client: Typesense client.
		query: Search query string.
		limit: Max results.
		filter_by: Typesense filter expression (e.g., 'countries:=[US] && hs_codes:=[7902]').
		facet_by: Comma-separated facet fields (e.g., 'countries,hs_codes,industries').

	Returns:
		Typesense search response dict with hits, facet_counts, etc.
	"""
	params = {
		"q": query,
		"query_by": "canonical_name,overview,products_services,product_descs,certifications,industries,address_hq",
		"per_page": limit,
		"highlight_full_fields": "overview,product_descs",
	}
	if filter_by:
		params["filter_by"] = filter_by
	if facet_by:
		params["facet_by"] = facet_by

	return client.collections[COLLECTION_NAME].documents.search(params)


async def hybrid_search(
	client: typesense.Client,
	query: str,
	limit: int = 20,
	filter_by: str = "",
	facet_by: str = "",
	vector_weight: float = 0.3,
) -> dict:
	"""Hybrid lexical + semantic search.

	Combines Typesense's built-in text search with vector similarity
	using the pre-computed Gemini embeddings.

	Args:
		client: Typesense client.
		query: Natural language query.
		limit: Max results.
		filter_by: Typesense filter expression.
		facet_by: Facet fields.
		vector_weight: Weight for vector search (0.0 = pure lexical, 1.0 = pure semantic).

	Returns:
		Typesense search response with hybrid-ranked results.
	"""
	from embeddings import generate_query_embedding

	query_embedding = await generate_query_embedding(query)

	params = {
		"q": query,
		"query_by": "canonical_name,overview,products_services,product_descs,certifications,industries,address_hq",
		"vector_query": f"embedding:([{','.join(str(v) for v in query_embedding)}], k:{limit})",
		"per_page": limit,
		"highlight_full_fields": "overview,product_descs",
	}
	if filter_by:
		params["filter_by"] = filter_by
	if facet_by:
		params["facet_by"] = facet_by

	return client.collections[COLLECTION_NAME].documents.search(params)


def get_company(client: typesense.Client, company_id: str) -> dict | None:
	"""Retrieve a single company document by ID."""
	try:
		return client.collections[COLLECTION_NAME].documents[company_id].retrieve()
	except ObjectNotFound:
		return None


def delete_company(client: typesense.Client, company_id: str) -> bool:
	"""Delete a company document."""
	try:
		client.collections[COLLECTION_NAME].documents[company_id].delete()
		return True
	except ObjectNotFound:
		return False


def url_to_company_id(url: str) -> str:
	"""Extract a clean domain name from a URL for use as company_id.

	Examples:
		'https://www.swiftbeef.com/about' -> 'swiftbeef.com'
		'http://www.example.co.uk'        -> 'example.co.uk'
		'https://jbsusa.com'              -> 'jbsusa.com'

	Strips protocol, www., path, query params, and port to produce
	a clean domain string safe for Typesense document IDs.
	"""
	try:
		parsed = urlparse(url)
		domain = parsed.netloc or parsed.path
		domain = domain.split(":")[0]  # strip port
		domain = domain.lower().strip(".")
		if domain.startswith("www."):
			domain = domain[4:]
		return domain or _name_to_id(url)
	except Exception:
		return _name_to_id(url)


def _name_to_id(name: str) -> str:
	"""Fallback: convert a company name to a stable document ID when no URL is available."""
	slug = name.upper().strip()
	slug = re.sub(r"[^\w\s]", "", slug)
	slug = re.sub(r"\s+", "_", slug).strip("_")
	return slug[:128]
