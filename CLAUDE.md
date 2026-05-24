# EXIM Skills — Design Principles

# Please update this file as memory everytime you are corrected by human about design and mindset

## The agent decides, scripts provide data

Scripts in this repo are **thin data fetchers**. They call APIs and return raw results. They do NOT:
- Score, rank, or threshold results
- Parse or interpret LLM responses (return raw text, let the agent read it)
- Build search queries (the agent constructs queries based on what it knows)
- Normalize, fuzzy-match, or validate company names against URLs
- Decide what's "high confidence" or "low confidence"
- Throw away data by catching errors and returning fallbacks

The orchestrator agent does all reasoning: reading results, deciding what to trust, constructing queries, verifying with agent-browser, and retrying with different approaches.

## Why: LLMs are better at reasoning than regex or hard code filter numbers

Hardcoded heuristics (fuzzy string matching, scoring formulas, confidence thresholds) are brittle and wrong for edge cases. The agent can read a page, understand context, recognize parent/subsidiary relationships, and adapt its strategy — things no amount of regex can do.


Each script is independently callable from CLI. The agent chains them as needed.

## What goes in scripts vs what the agent does

**Scripts handle:** API calls, HTTP requests, directory domain filtering (static list)

**Agent handles:** query construction, result evaluation, confidence assessment, URL verification, retry decisions, name normalization, search query refinement

## LLM and API calls: always use llm.py

All LLM and embedding API calls MUST go through `.claude/skills/llm.py`. Never call provider SDKs (openai, anthropic, google) directly. `llm.py` is the unified abstraction — it handles provider switching, config loading from `models.yaml`, token tracking, and cost calculation. To switch models, change a config key in `.env`, not code.

```python
from llm import create_agent_from_config, get_embedding

# LLM completion
agent = create_agent_from_config("gemini-2.5-flash")  # or any key from models.yaml
response = await agent.async_completions([{"role": "user", "content": "..."}])

# Embeddings
result = await get_embedding("gemini-embedding-001", text="...", task_type="RETRIEVAL_DOCUMENT")
```
