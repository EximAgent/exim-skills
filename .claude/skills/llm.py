"""Unified LLM provider abstraction for trade company agent skills.

Adapted from simple-llm-api (https://github.com/centerforaisafety/simple-llm-api).
Follows the provider/model-name pattern for easy provider switching.

Models are configured in models.yaml — switch providers by changing a config key
in .env without editing any code.

Usage:
    from llm import get_llm_agent_class, get_agent_config

    # Load config and create agent
    config = get_agent_config("gemini-2.5-flash")
    agent = get_llm_agent_class(
        model=config["model"],
        generation_config=config.get("generation_config", {}),
    )

    # Sync completion
    response = agent.completions([{"role": "user", "content": "Hello"}])
    print(response.content)
    print(response.token_usage.cost)

    # Async completion
    response = await agent.async_completions([{"role": "user", "content": "Hello"}])

    # Get LangChain LLM for browser-use
    from llm import get_langchain_llm
    llm = get_langchain_llm("gpt-4o")
"""

from __future__ import annotations

import asyncio
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict

import httpx
import yaml
from pydantic import BaseModel

try:
	import litellm
	litellm.suppress_debug_info = True
	_HAS_LITELLM = True
except ImportError:
	_HAS_LITELLM = False

from dotenv import load_dotenv
load_dotenv()

TIMEOUT = 600

# Resolve models.yaml relative to this file
_MODELS_YAML = Path(__file__).parent / "models.yaml"


# =================== Config Loading ===================

def get_agent_config(model: str, models_config_path: str | None = None) -> Dict[str, Any]:
	"""Load model configuration from models.yaml.

	Expected config format:
	    gemini-2.5-flash:
	      model: gemini/gemini-2.5-flash
	      generation_config:
	        temperature: 0.1
	        max_tokens: 100

	Args:
	    model: Config key to load (e.g., 'gemini-2.5-flash').
	    models_config_path: Override path to models.yaml.

	Returns:
	    Dict with 'model' (provider/name) and 'generation_config'.
	"""
	config_path = models_config_path or str(_MODELS_YAML)
	with open(config_path) as f:
		model_configs = yaml.safe_load(f)

	assert model in model_configs, (
		f"Model '{model}' not found in {config_path}. "
		f"Available: {list(model_configs.keys())}"
	)
	return model_configs[model]


# =================== Data Models ===================

class TokenUsage(BaseModel):
	input_tokens: int = 0
	output_tokens: int = 0
	total_tokens: int = 0
	cached_tokens: int = 0
	cost: float = 0.0


class LLMResponse(BaseModel):
	content: str | None = None
	reasoning_content: str | None = None
	token_usage: TokenUsage | None = None
	raw: dict | None = None


# =================== Base Agent ===================

class LLMAgent(ABC):
	def __init__(self, model: str, provider: str | None = None):
		self.model = model
		self.provider = provider
		self.all_token_usage = TokenUsage()
		self.max_token_usage = TokenUsage()
		self._usage_lock = asyncio.Lock()

	def _update_usage(self, token_usage: TokenUsage):
		self.all_token_usage = sum_token_usage([self.all_token_usage, token_usage])
		self.max_token_usage = get_max_token_usage([self.max_token_usage, token_usage])

	async def _update_usage_async(self, token_usage: TokenUsage):
		async with self._usage_lock:
			self.all_token_usage = sum_token_usage([self.all_token_usage, token_usage])
			self.max_token_usage = get_max_token_usage([self.max_token_usage, token_usage])

	def _calculate_cost(self, response: Any) -> float:
		if not _HAS_LITELLM:
			return 0.0

		usage = getattr(response, 'usage', None)
		if usage and not hasattr(usage, 'total_tokens'):
			usage.total_tokens = getattr(usage, 'input_tokens', 0) + getattr(usage, 'output_tokens', 0)

		try:
			cost = litellm.cost_calculator.completion_cost(
				completion_response=response,
				model=self.model,
				custom_llm_provider=self.provider,
			)
		except Exception:
			cost = 0.0
		return cost

	@abstractmethod
	def _completions(self, messages) -> LLMResponse:
		raise NotImplementedError

	@abstractmethod
	async def _async_completions(self, messages) -> LLMResponse:
		raise NotImplementedError

	def completions(self, messages: list[dict], **kwargs) -> LLMResponse:
		return self._completions(messages, **kwargs)

	async def async_completions(self, messages: list[dict], **kwargs) -> LLMResponse:
		return await self._async_completions(messages, **kwargs)


# =================== OpenAI Agent ===================

class OpenAIAgent(LLMAgent):
	def __init__(
		self,
		model: str,
		api_key_env: str = "OPENAI_API_KEY",
		api_base_url: str | None = None,
		provider: str = "openai",
		**generation_config,
	):
		super().__init__(model=model, provider=provider)
		import openai

		api_key = os.getenv(api_key_env)
		if not api_key:
			raise ValueError(f"API key not found in environment variable {api_key_env}")

		if api_base_url is None:
			api_base_url = os.getenv("OPENAI_API_BASE_URL", "https://api.openai.com/v1")

		self.client = openai.OpenAI(api_key=api_key, base_url=api_base_url, timeout=TIMEOUT)
		self.async_client = openai.AsyncOpenAI(api_key=api_key, base_url=api_base_url, timeout=TIMEOUT)
		self.generation_config = generation_config

	def _preprocess_messages(self, messages: list[dict]) -> list[dict]:
		return messages

	def _parse_response(self, response):
		message = response.choices[0].message
		content = message.content
		reasoning_content = getattr(message, "reasoning_content", None)
		usage = response.usage
		cached_tokens = getattr(getattr(usage, "prompt_tokens_details", None), "cached_tokens", 0)

		cost = self._calculate_cost(response)
		token_usage = TokenUsage(
			input_tokens=usage.prompt_tokens,
			output_tokens=usage.completion_tokens,
			total_tokens=usage.total_tokens,
			cached_tokens=cached_tokens,
			cost=cost,
		)

		raw_response = response.model_dump() if hasattr(response, "model_dump") else {}
		return content, reasoning_content, token_usage, raw_response

	def _completions(self, messages: list[dict], **kwargs) -> LLMResponse:
		messages = self._preprocess_messages(messages)
		response = self.client.chat.completions.create(
			model=self.model,
			messages=messages,
			**self.generation_config,
			**kwargs,
		)
		content, reasoning_content, token_usage, raw_response = self._parse_response(response)
		self._update_usage(token_usage)
		return LLMResponse(content=content, reasoning_content=reasoning_content, token_usage=token_usage, raw=raw_response)

	async def _async_completions(self, messages: list[dict], **kwargs) -> LLMResponse:
		messages = self._preprocess_messages(messages)
		response = await self.async_client.chat.completions.create(
			model=self.model,
			messages=messages,
			**self.generation_config,
			**kwargs,
		)
		content, reasoning_content, token_usage, raw_response = self._parse_response(response)
		await self._update_usage_async(token_usage)
		return LLMResponse(content=content, reasoning_content=reasoning_content, token_usage=token_usage, raw=raw_response)


# =================== Gemini Agent ===================

class GeminiAgent(OpenAIAgent):
	"""Gemini via OpenAI-compatible endpoint.

	Supports both Google AI API and Vertex AI.
	"""
	def __init__(
		self,
		model: str,
		api_key_env: str = "GEMINI_API_KEY",
		api_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/",
		provider: str = "gemini",
		vertexai: bool = False,
		**generation_config,
	):
		if not vertexai:
			super().__init__(
				model=model,
				api_key_env=api_key_env,
				api_base_url=api_base_url,
				provider=provider,
				**generation_config,
			)
		else:
			from google.auth import default
			from google.auth.transport.requests import Request
			import openai

			project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
			location = os.environ.get("GOOGLE_CLOUD_LOCATION")
			assert project_id and location, (
				"GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION must be set for Vertex AI"
			)

			credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
			credentials.refresh(Request())

			api_host = f"{location}-aiplatform.googleapis.com" if location != "global" else "aiplatform.googleapis.com"
			api_base_url = f"https://{api_host}/v1/projects/{project_id}/locations/{location}/endpoints/openapi"
			api_key = credentials.token

			if "/" not in model:
				model = f"google/{model}"

			LLMAgent.__init__(self, model=model, provider="vertex_ai")
			self.client = openai.OpenAI(api_key=api_key, base_url=api_base_url, timeout=TIMEOUT)
			self.async_client = openai.AsyncOpenAI(api_key=api_key, base_url=api_base_url, timeout=TIMEOUT)
			self.generation_config = generation_config


# =================== Grok Agent ===================

class GrokAgent(OpenAIAgent):
	def __init__(
		self,
		model: str,
		api_key_env: str = "XAI_API_KEY",
		api_base_url: str = "https://api.x.ai/v1",
		provider: str = "xai",
		**generation_config,
	):
		super().__init__(
			model=model,
			api_key_env=api_key_env,
			api_base_url=api_base_url,
			provider=provider,
			**generation_config,
		)
		self.needs_flattening = False

	def _flatten_conversation(self, messages: list[dict]) -> list[dict]:
		if not messages:
			return messages

		system_msgs = [m for m in messages if m["role"] == "system"]
		conv_msgs = [m for m in messages if m["role"] != "system"]

		if len(conv_msgs) <= 4:
			return messages

		def get_text(content):
			if isinstance(content, str):
				return content
			if isinstance(content, list):
				return " ".join(item.get("text", str(item)) for item in content if isinstance(item, dict))
			return str(content)

		flattened = "\n\n".join(f"{m['role'].upper()}: {get_text(m['content'])}" for m in conv_msgs)
		return system_msgs + [{"role": "user", "content": flattened}]

	def _preprocess_messages(self, messages: list[dict]) -> list[dict]:
		if self.needs_flattening:
			return self._flatten_conversation(messages)
		return messages


# =================== Anthropic Agent ===================

class AnthropicAgent(LLMAgent):
	def __init__(
		self,
		model: str,
		use_cache: bool = False,
		vertexai: bool = False,
		provider: str = "anthropic",
		**generation_config,
	):
		provider = "vertex_ai" if vertexai else provider
		super().__init__(model=model, provider=provider)
		import anthropic

		if vertexai:
			region = os.getenv("GOOGLE_CLOUD_LOCATION", "global")
			project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
			assert project_id, "GOOGLE_CLOUD_PROJECT must be set for Vertex AI"
			self.client = anthropic.AnthropicVertex(region=region, project_id=project_id, timeout=TIMEOUT)
			self.async_client = anthropic.AsyncAnthropicVertex(region=region, project_id=project_id, timeout=TIMEOUT)
		else:
			assert os.getenv("ANTHROPIC_API_KEY"), "ANTHROPIC_API_KEY environment variable not set"
			self.client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), timeout=TIMEOUT)
			self.async_client = anthropic.AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), timeout=TIMEOUT)

		self.use_cache = use_cache
		self.generation_config = generation_config

	def _preprocess_messages(self, messages: list[dict]) -> tuple:
		system = None
		caching_messages = []

		for i, message in enumerate(messages):
			if message["role"] == "system":
				system = [dict(type="text", text=message["content"])]
			else:
				new_block = {"role": message["role"]}

				if isinstance(message["content"], str):
					if self.use_cache and i == len(messages) - 1:
						content = [dict(type="text", text=message["content"], cache_control={"type": "ephemeral"})]
					else:
						content = [dict(type="text", text=message["content"])]
				elif isinstance(message["content"], list):
					content = []
					for item in message["content"]:
						if item["type"] == "text":
							if self.use_cache and i == len(messages) - 1 and item == message["content"][-1]:
								content.append(dict(type="text", text=item["text"], cache_control={"type": "ephemeral"}))
							else:
								content.append(dict(type="text", text=item["text"]))
						elif item["type"] == "image_url":
							image_url = item["image_url"]["url"]
							if image_url.startswith("data:image/"):
								media_type, base64_data = image_url.split(";base64,")
								media_type = media_type.replace("data:", "")
								content.append({
									"type": "image",
									"source": {
										"type": "base64",
										"media_type": media_type,
										"data": base64_data,
									},
								})
				else:
					if self.use_cache and i == len(messages) - 1:
						content = [dict(type="text", text=str(message["content"]), cache_control={"type": "ephemeral"})]
					else:
						content = [dict(type="text", text=str(message["content"]))]

				new_block["content"] = content
				caching_messages.append(new_block)

		return system, caching_messages

	def _parse_response(self, response):
		text_blocks = [block for block in response.content if hasattr(block, "text")]
		content = text_blocks[-1].text if text_blocks else None
		usage = response.usage
		cost = self._calculate_cost(response)

		token_usage = TokenUsage(
			input_tokens=usage.input_tokens + usage.cache_creation_input_tokens,
			output_tokens=usage.output_tokens,
			cached_tokens=usage.cache_read_input_tokens,
			total_tokens=usage.input_tokens + usage.output_tokens,
			cost=cost,
		)

		raw_response = response.model_dump() if hasattr(response, "model_dump") else {}
		return content, None, token_usage, raw_response

	def _completions(self, messages: list[dict], **kwargs) -> LLMResponse:
		system, messages = self._preprocess_messages(messages)
		api_kwargs: dict = {
			"model": self.model,
			"messages": messages,
			**self.generation_config,
		}
		if system is not None:
			api_kwargs["system"] = system

		response = self.client.messages.create(**api_kwargs)
		content, reasoning_content, token_usage, raw_response = self._parse_response(response)
		self._update_usage(token_usage)
		return LLMResponse(content=content, reasoning_content=reasoning_content, token_usage=token_usage, raw=raw_response)

	async def _async_completions(self, messages: list[dict], **kwargs) -> LLMResponse:
		system, messages = self._preprocess_messages(messages)
		api_kwargs: dict = {
			"model": self.model,
			"messages": messages,
			**self.generation_config,
		}
		if system is not None:
			api_kwargs["system"] = system

		response = await self.async_client.messages.create(**api_kwargs)
		content, reasoning_content, token_usage, raw_response = self._parse_response(response)
		await self._update_usage_async(token_usage)
		return LLMResponse(content=content, reasoning_content=reasoning_content, token_usage=token_usage, raw=raw_response)


# =================== Factory ===================

def get_llm_agent_class(model: str, generation_config: dict | None = None, **kwargs) -> LLMAgent:
	"""Create an LLM agent from a provider/model string.

	Args:
	    model: Provider/model string (e.g., 'openai/gpt-4o', 'gemini/gemini-2.5-flash').
	    generation_config: Provider-specific generation parameters.
	    **kwargs: Additional kwargs passed to the agent constructor.

	Returns:
	    LLMAgent instance.
	"""
	provider, model_name = model.split("/", 1)

	provider_to_class = {
		"openai": OpenAIAgent,
		"anthropic": AnthropicAgent,
		"gemini": GeminiAgent,
		"xai": GrokAgent,
	}
	assert provider in provider_to_class, (
		f"Provider '{provider}' not supported. Available: {list(provider_to_class.keys())}"
	)
	return provider_to_class[provider](model=model_name, **(generation_config or {}), **kwargs)


# =================== Convenience Helpers ===================

def create_agent_from_config(config_key: str, models_config_path: str | None = None) -> LLMAgent:
	"""Create an LLM agent from a models.yaml config key.

	Args:
	    config_key: Key in models.yaml (e.g., 'gemini-2.5-flash').
	    models_config_path: Override path to models.yaml.

	Returns:
	    LLMAgent instance.
	"""
	config = get_agent_config(config_key, models_config_path)
	return get_llm_agent_class(
		model=config["model"],
		generation_config=config.get("generation_config", {}),
	)


def get_langchain_llm(config_key: str, models_config_path: str | None = None):
	"""Get a LangChain chat model instance for browser-use agent.

	Args:
	    config_key: Key in models.yaml (e.g., 'gpt-4o', 'claude-sonnet-4').

	Returns:
	    LangChain BaseChatModel instance (ChatOpenAI or ChatAnthropic).
	"""
	config = get_agent_config(config_key, models_config_path)
	provider, model_name = config["model"].split("/", 1)
	gen_config = config.get("generation_config", {})

	if provider in ("openai", "gemini", "xai"):
		from langchain_openai import ChatOpenAI

		api_key_env = gen_config.get("api_key_env", {
			"openai": "OPENAI_API_KEY",
			"gemini": "GEMINI_API_KEY",
			"xai": "XAI_API_KEY",
		}.get(provider, "OPENAI_API_KEY"))

		api_key = os.getenv(api_key_env)
		if not api_key:
			raise ValueError(f"API key not found in env var '{api_key_env}'")

		base_url = gen_config.get("api_base_url", {
			"openai": "https://api.openai.com/v1",
			"gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
			"xai": "https://api.x.ai/v1",
		}.get(provider))

		return ChatOpenAI(
			model=model_name,
			api_key=api_key,
			base_url=base_url,
			temperature=gen_config.get("temperature", 0),
		)
	elif provider == "anthropic":
		from langchain_anthropic import ChatAnthropic
		api_key = os.getenv("ANTHROPIC_API_KEY")
		if not api_key:
			raise ValueError("ANTHROPIC_API_KEY not set")
		return ChatAnthropic(
			model=model_name,
			api_key=api_key,
			temperature=gen_config.get("temperature", 0),
		)
	else:
		raise ValueError(
			f"LangChain integration not supported for provider '{provider}'. "
			f"Use 'openai', 'gemini', 'anthropic', or 'xai'."
		)


# =================== Embedding Helper ===================

async def get_embedding(
	config_key: str,
	text: str,
	task_type: str = "RETRIEVAL_DOCUMENT",
	models_config_path: str | None = None,
) -> dict:
	"""Get an embedding vector from a configured embedding model.

	Args:
	    config_key: Key in models.yaml (e.g., 'text-embedding-004').
	    text: Text to embed.
	    task_type: Task type hint (RETRIEVAL_DOCUMENT or RETRIEVAL_QUERY).
	    models_config_path: Override path to models.yaml.

	Returns:
	    Dict with 'values' (list[float]), 'dim' (int), and 'model' (str).
	"""
	config = get_agent_config(config_key, models_config_path)
	provider, model_name = config["model"].split("/", 1)
	gen_config = config.get("generation_config", {})

	if provider == "gemini":
		api_key_env = gen_config.get("api_key_env", "GEMINI_API_KEY")
		api_key = os.environ.get(api_key_env)
		assert api_key, f"{api_key_env} environment variable is required"

		url = (
			f"https://generativelanguage.googleapis.com/v1beta/models/"
			f"{model_name}:embedContent?key={api_key}"
		)
		async with httpx.AsyncClient(timeout=30.0) as client:
			resp = await client.post(url, json={
				"content": {"parts": [{"text": text[:8000]}]},
				"taskType": task_type,
			})
			resp.raise_for_status()
			data = resp.json()

		values = data["embedding"]["values"]
		return {"values": values, "dim": len(values), "model": f"gemini/{model_name}"}

	elif provider == "openai":
		api_key_env = gen_config.get("api_key_env", "OPENAI_API_KEY")
		api_key = os.environ.get(api_key_env)
		assert api_key, f"{api_key_env} environment variable is required"

		base_url = gen_config.get("api_base_url", "https://api.openai.com/v1")
		url = f"{base_url.rstrip('/')}/embeddings"

		async with httpx.AsyncClient(timeout=30.0) as client:
			resp = await client.post(
				url,
				headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
				json={"model": model_name, "input": text[:8000]},
			)
			resp.raise_for_status()
			data = resp.json()

		values = data["data"][0]["embedding"]
		return {"values": values, "dim": len(values), "model": f"openai/{model_name}"}

	else:
		raise ValueError(f"Embedding not supported for provider: {provider}")


# =================== Utils ===================

def sum_token_usage(token_usages: list[TokenUsage]) -> TokenUsage:
	return TokenUsage(
		input_tokens=sum(t.input_tokens for t in token_usages),
		output_tokens=sum(t.output_tokens for t in token_usages),
		total_tokens=sum(t.total_tokens for t in token_usages),
		cached_tokens=sum(t.cached_tokens for t in token_usages),
		cost=sum(t.cost for t in token_usages),
	)


def get_max_token_usage(token_usages: list[TokenUsage]) -> TokenUsage:
	return TokenUsage(
		input_tokens=max(t.input_tokens for t in token_usages),
		output_tokens=max(t.output_tokens for t in token_usages),
		total_tokens=max(t.total_tokens for t in token_usages),
		cached_tokens=max(t.cached_tokens for t in token_usages),
		cost=max(t.cost for t in token_usages),
	)
