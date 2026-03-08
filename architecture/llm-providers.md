# LLM Provider System

NexusCode uses a provider abstraction layer (`src/llm/`) that routes LLM calls to the
appropriate SDK based on the model name. No consumer code imports Anthropic/OpenAI SDKs
directly — all calls go through `get_client_for_model()`.

---

## Supported Providers and Models

| Provider | Models | API Key |
|----------|--------|---------|
| **Anthropic** | `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` |
| **OpenAI** | `gpt-4o`, `gpt-4o-mini`, `o3`, `o4-mini` | `OPENAI_API_KEY` |
| **xAI / Grok** | `grok-3`, `grok-3-mini` | `GROK_API_KEY` |
| **Ollama** | Any model name (e.g. `llama3.3`, `qwen2.5-coder`) | `OLLAMA_BASE_URL` + `OLLAMA_MODELS` |

---

## Provider Detection

```python
# src/llm/client.py
def get_client_for_model(model: str):
    if model.startswith("claude-"):
        return anthropic_client
    elif model.startswith("grok-"):
        return openai.AsyncOpenAI(base_url="https://api.x.ai/v1", api_key=GROK_API_KEY)
    elif is_ollama_model(model):
        return openai.AsyncOpenAI(base_url=OLLAMA_BASE_URL, api_key="ollama")
    else:
        return openai_client   # GPT-4o, o3, etc.

def is_ollama_model(model: str) -> bool:
    if not settings.ollama_base_url or not settings.ollama_models:
        return False
    return model in [m.strip() for m in settings.ollama_models.split(",")]
```

---

## Concurrency Control

Each provider has its own semaphore to prevent rate-limit cascades:

```python
# src/llm/client.py
semaphore = asyncio.Semaphore(5)   # max 5 concurrent LLM calls across all providers
```

All LLM calls in `AgentLoop` acquire this semaphore before calling the API:

```python
async with semaphore:
    for attempt in range(MAX_RETRIES + 1):
        try:
            response = await client.messages.create(**params)
            break
        except APIStatusError as exc:
            if _is_retryable(exc) and attempt < MAX_RETRIES:
                wait = min(get_retry_after(exc) or (5 * 2**attempt), 120)
                await asyncio.sleep(wait)
            else:
                raise
```

---

## Retry Strategy

```python
MAX_RETRIES = 3
RETRYABLE_CODES = (429, 529)  # rate limit + overload

def _is_retryable(exc) -> bool:
    # Standard HTTP 429/529
    if exc.status_code in RETRYABLE_CODES:
        return True
    # Anthropic overload delivered as SSE error event inside a 200 stream
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        return body.get("error", {}).get("type") == "overloaded_error"
    return False

def get_retry_after(exc) -> float | None:
    # Honor Retry-After header if present
    headers = getattr(exc, "response", {}).headers
    ra = headers.get("retry-after")
    return float(ra) if ra else None
```

**Backoff formula:** `min(retry_after or (5 * 2^attempt), 120)` seconds.
- Attempt 1: 5s (or Retry-After)
- Attempt 2: 10s
- Attempt 3: 20s
- Cap: 120s

---

## Provider-Specific Features

### Anthropic (Claude)

| Feature | Support |
|---------|---------|
| Streaming | ✅ `client.messages.stream()` |
| Extended thinking | ✅ `{"type": "enabled", "budget_tokens": N}` |
| Prompt caching | ✅ `cache_control: {type: "ephemeral"}` on last tool |
| Web search | ✅ `web_search_20250305` tool (planning mode) |
| Tool use | ✅ Native |
| Vision | ✅ (not currently used) |

**Planning Mode with extended thinking:**
```python
if config.thinking_budget > 0 and not force_final:
    params["thinking"] = {"type": "enabled", "budget_tokens": config.thinking_budget}
    # Must use streaming — non-streaming times out with large max_tokens
    async with client.messages.stream(**params) as stream:
        response = await stream.get_final_message()
```

**Web research** (Anthropic-only, Planning Mode):
```python
# Uses Anthropic's built-in web search tool
# Gated on provider — only available when using Claude models
if is_anthropic_model(model) and web_research:
    plan_context = await retrieve_planning_context(query, web_research=True)
```

### OpenAI (GPT-4o, o3)

| Feature | Support |
|---------|---------|
| Streaming | ✅ `client.chat.completions.stream()` |
| Tool use | ✅ Function calling |
| Vision | ✅ (not currently used) |
| Web search | ❌ Not integrated |
| Prompt caching | ❌ Not supported |

OpenAI models use the `chat.completions` API internally. Tool schemas are converted:
```python
# Anthropic format → OpenAI format
# input_schema → parameters
# "type": "object" required
```

### xAI / Grok

Grok uses the OpenAI-compatible API at `https://api.x.ai/v1`. Same code path as OpenAI
with a different base URL and API key:

```python
AsyncOpenAI(
    base_url="https://api.x.ai/v1",
    api_key=settings.grok_api_key,
)
```

### Ollama (Local Models)

Ollama uses the OpenAI-compatible API at `OLLAMA_BASE_URL` (default `http://localhost:11434/v1`):
- **No prompt caching** (disabled automatically)
- Tool schemas converted to OpenAI format
- All locally-running models listed in `OLLAMA_MODELS` (comma-separated)

---

## Model Selection

### Default Model

```python
# src/config.py
default_model: str = "claude-sonnet-4-6"
```

### Per-Request Override

All three AI endpoints accept an optional `model` field:

```json
POST /ask    {"query": "...", "model": "gpt-4o"}
POST /plan   {"query": "...", "model": "claude-opus-4-6"}
```

### Available Models Endpoint

```
GET /models
→ Returns models with configured API keys only

[
  {"model": "claude-sonnet-4-6", "provider": "anthropic"},
  {"model": "claude-opus-4-6",   "provider": "anthropic"},
  {"model": "gpt-4o",            "provider": "openai"},
  {"model": "llama3.3",          "provider": "ollama"}
]
```

---

## Adding a New Provider

1. Add API key to `src/config.py`
2. Update `get_client_for_model()` to detect new model prefix
3. If OpenAI-compatible: create `AsyncOpenAI(base_url=..., api_key=...)`
4. If custom protocol: implement `LLMProvider` Protocol from `src/llm/base.py`
5. Add models to `GET /models` response in `src/api/app.py`

---

## Cost Considerations

| Provider | Cheapest capable model | Notes |
|----------|----------------------|-------|
| Anthropic | `claude-haiku-4-5-20251001` | 10x cheaper than Sonnet; good for simple ask |
| OpenAI | `gpt-4o-mini` | Very cheap; limited reasoning |
| xAI | `grok-3-mini` | Cost-effective for coding tasks |
| Ollama | Any | Free; quality depends on model size |

**Planning Mode** defaults to `claude-sonnet-4-6` for best results.
**Ask Mode** defaults to `claude-sonnet-4-6` but `claude-haiku-4-5-20251001` works for simple questions.
