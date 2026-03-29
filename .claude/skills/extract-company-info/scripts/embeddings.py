"""Embedding helper using the unified LLM provider abstraction from llm.py.

Adapted from simple-llm-api pattern. Models are configured in models.yaml.
Supports Gemini and OpenAI embedding models — switch by changing the config key.

Configure via env var:
    EMBEDDING_MODEL_CONFIG=text-embedding-004            # Gemini (default)
    EMBEDDING_MODEL_CONFIG=openai-text-embedding-3-small # OpenAI

Requires: GEMINI_API_KEY or OPENAI_API_KEY depending on provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import httpx

# Add skills root so we can import the shared llm module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from llm import get_agent_config, get_embedding as _llm_get_embedding

# Config key in models.yaml — override via env var
EMBEDDING_CONFIG = os.environ.get("EMBEDDING_MODEL_CONFIG", "gemini-embedding-001")

# Read dim from models.yaml, fallback to 768
_config = get_agent_config(EMBEDDING_CONFIG)
EMBEDDING_DIM = _config.get("generation_config", {}).get("embedding_dim", 768)

CACHE_DIR = Path.home() / ".cache" / "company_embeddings"


async def generate_embedding(text: str, use_cache: bool = True) -> list[float]:
	"""Generate an embedding for a single text.

	Args:
		text: Text to embed.
		use_cache: Whether to cache results on disk.

	Returns:
		List of floats (dimension depends on configured model).
	"""
	if not text or not text.strip():
		return [0.0] * EMBEDDING_DIM

	if use_cache:
		cached = _load_cache(text)
		if cached is not None:
			return cached

	response = await _llm_get_embedding(EMBEDDING_CONFIG, text, task_type="RETRIEVAL_DOCUMENT")
	embedding = response["values"]

	if use_cache:
		_save_cache(text, embedding)

	return embedding


async def generate_embeddings(
	texts: list[str],
	use_cache: bool = True,
) -> list[list[float]]:
	"""Generate embeddings for multiple texts.

	Calls the single-text API sequentially. For Gemini batch API,
	use generate_embeddings_batch_gemini().

	Args:
		texts: List of texts to embed.
		use_cache: Whether to cache results on disk.

	Returns:
		List of embedding vectors.
	"""
	if not texts:
		return []

	results: list[list[float]] = []
	for text in texts:
		emb = await generate_embedding(text, use_cache=use_cache)
		results.append(emb)
	return results


async def generate_embeddings_batch_gemini(
	texts: list[str],
	use_cache: bool = True,
) -> list[list[float]]:
	"""Batch embedding generation via Gemini's batchEmbedContents API.

	More efficient than sequential calls for large batches. Falls back
	to sequential if not using a Gemini embedding model.
	"""
	if not texts:
		return []

	config = get_agent_config(EMBEDDING_CONFIG)
	provider, model_name = config["model"].split("/", 1)

	if provider != "gemini":
		return await generate_embeddings(texts, use_cache)

	# Check cache for each text
	results: list[list[float] | None] = [None] * len(texts)
	to_generate: list[tuple[int, str]] = []

	for i, text in enumerate(texts):
		if not text or not text.strip():
			results[i] = [0.0] * EMBEDDING_DIM
			continue
		if use_cache:
			cached = _load_cache(text)
			if cached is not None:
				results[i] = cached
				continue
		to_generate.append((i, text))

	if not to_generate:
		return results  # type: ignore

	# Resolve API key
	gen_config = config.get("generation_config", {})
	api_key_env = gen_config.get("api_key_env", "GEMINI_API_KEY")
	api_key = os.environ.get(api_key_env)
	assert api_key, f"{api_key_env} environment variable is required"

	batch_url = (
		f"https://generativelanguage.googleapis.com/v1beta/models/"
		f"{model_name}:batchEmbedContents?key={api_key}"
	)

	# Process in batches of 100
	for batch_start in range(0, len(to_generate), 100):
		batch = to_generate[batch_start:batch_start + 100]
		requests_payload = [
			{
				"model": f"models/{model_name}",
				"content": {"parts": [{"text": text[:8000]}]},
				"taskType": "RETRIEVAL_DOCUMENT",
			}
			for _, text in batch
		]

		async with httpx.AsyncClient(timeout=60.0) as client:
			resp = await client.post(
				batch_url,
				headers={"Content-Type": "application/json"},
				json={"requests": requests_payload},
			)
			resp.raise_for_status()
			data = resp.json()

		for j, embedding_data in enumerate(data["embeddings"]):
			idx, text = batch[j]
			embedding = embedding_data["values"]
			results[idx] = embedding
			if use_cache:
				_save_cache(text, embedding)

	return results  # type: ignore


async def generate_query_embedding(query: str) -> list[float]:
	"""Generate an embedding optimized for search queries.

	Uses RETRIEVAL_QUERY task type for better search relevance.
	"""
	if not query or not query.strip():
		return [0.0] * EMBEDDING_DIM

	response = await _llm_get_embedding(EMBEDDING_CONFIG, query, task_type="RETRIEVAL_QUERY")
	return response["values"]


def _cache_key(text: str) -> str:
	return hashlib.sha256(text.encode()).hexdigest()[:16]


def _load_cache(text: str) -> list[float] | None:
	path = CACHE_DIR / f"{_cache_key(text)}.json"
	if path.exists():
		try:
			return json.loads(path.read_text())
		except (json.JSONDecodeError, OSError):
			return None
	return None


def _save_cache(text: str, embedding: list[float]) -> None:
	try:
		CACHE_DIR.mkdir(parents=True, exist_ok=True)
		path = CACHE_DIR / f"{_cache_key(text)}.json"
		path.write_text(json.dumps(embedding))
	except OSError:
		pass
