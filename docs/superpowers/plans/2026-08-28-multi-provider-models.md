# Multi-Provider Model Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let extraction and draft generation run against Anthropic, OpenAI, Grok, HuggingFace, or any local OpenAI-compatible server (Ollama, vLLM, LM Studio, llama.cpp), selected by one `PROVIDER` setting, without weakening the verbatim source-text gate.

**Architecture:** The runner stops emitting one vendor's message format and builds a neutral content IR instead; each client translates that IR to its own wire format. A registry maps `PROVIDER` to a base URL and key, and `build_client(settings)` is the only place a client is constructed. Provider and model are recorded on every extraction, and a computed verification rate exposes models that cannot quote the document.

**Tech Stack:** Python 3.11+, httpx (already installed as an `anthropic` SDK dependency), the `anthropic` SDK, pydantic, pytest.

**Spec:** `docs/superpowers/specs/2026-08-28-multi-provider-models-design.md`

## Global Constraints

- Python 3.11+. PostgreSQL only — never SQLite, including in tests.
- **The source-text gate does not move.** `renewal/extract/validate.py` is not edited by this plan. No per-provider confidence threshold, no fuzzy matching, no bypass flag. A model that cannot quote the document produces `needs_review` fields and blocks promotion; that is the correct outcome.
- **No migration.** Every change here is insert-only or computed on read. If a task appears to need a schema change, stop and ask.
- **Every table stays insert-only.** No `UPDATE`, no `DELETE` in application code.
- **Never log extracted content.** Log document ids, blob hashes, provider and model names only.
- Temperature is `0` for every provider, every call.
- One provider governs both extraction and drafting. Do not add a separate draft provider.
- The application never downloads a model, spawns an inference server, or health-checks a runtime it did not start.
- No test in the default run may make a network call. The eval harness stays behind `-m eval`.
- Do not build: streaming, retry/backoff policy, token or cost accounting, per-provider JSON modes, a model picker in the web UI.

---

## File Structure

| File | Responsibility |
|---|---|
| `renewal/providers.py` | **New.** Neutral content IR constructors, per-provider translation, both clients, the preset registry, `build_client`. |
| `tests/test_providers.py` | **New.** IR translation, HTTP transport against a stub, registry resolution and failure. |
| `renewal/config.py` | Adds `provider`, `llm_base_url`, `llm_api_key`, and `PROVIDER_KEY_ENV`. |
| `renewal/extract/runner.py` | Emits neutral IR; `AnthropicClient` moves out; `model_id` records `provider:model`. |
| `renewal/extract/validate.py` | Gains `verification_rate` only. The gate itself is untouched. |
| `renewal/app.py` | Builds its client via `build_client`. |
| `renewal/web.py` | Puts the verification rate into the review context. |
| `renewal/templates/run_review.html` | Displays it per extraction. |
| `evals/accuracy.py` | `baseline_path` helper; verification rate in the report. |
| `evals/test_extraction.py` | Client via `build_client`; per-provider-and-model baseline. |
| `scripts/compare_versions.py` | Compares two baseline files. No API calls. |
| `.env.example` | `PROVIDER` and the per-provider key variables. |
| `pyproject.toml` | `httpx` promoted to a runtime dependency. |
| `PRIVACY.md` | Rewritten to describe both third-party and local modes. |

---

## Task 1: Neutral content IR and translation

**Files:**
- Create: `renewal/providers.py`, `tests/test_providers.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `text_block(text: str) -> dict`, `image_block(png: bytes) -> dict`, `to_anthropic(content: list[dict]) -> list[dict]`, `to_openai(content: list[dict]) -> list[dict]`. IR block shapes: `{"type": "text", "text": str}` and `{"type": "image_png", "data": bytes}`.

- [ ] **Step 1: Write the failing tests**

`tests/test_providers.py`:

```python
import base64

import pytest

from renewal.providers import (
    image_block,
    text_block,
    to_anthropic,
    to_openai,
)

PNG = b"\x89PNG\r\n\x1a\nfake"


def test_text_block_is_passed_through_by_both_translators():
    content = [text_block("read this page")]
    assert to_anthropic(content) == [{"type": "text", "text": "read this page"}]
    assert to_openai(content) == [{"type": "text", "text": "read this page"}]


def test_anthropic_renders_an_image_as_a_base64_source_block():
    block = to_anthropic([image_block(PNG)])[0]
    assert block["type"] == "image"
    assert block["source"]["type"] == "base64"
    assert block["source"]["media_type"] == "image/png"
    assert base64.b64decode(block["source"]["data"]) == PNG


def test_openai_renders_an_image_as_a_data_uri():
    block = to_openai([image_block(PNG)])[0]
    assert block["type"] == "image_url"
    prefix = "data:image/png;base64,"
    url = block["image_url"]["url"]
    assert url.startswith(prefix)
    assert base64.b64decode(url[len(prefix):]) == PNG


def test_order_is_preserved_across_mixed_content():
    content = [text_block("first"), image_block(PNG), text_block("last")]
    for rendered in (to_anthropic(content), to_openai(content)):
        assert [b["type"] for b in rendered][0] == "text"
        assert [b["type"] for b in rendered][2] == "text"
        assert len(rendered) == 3


@pytest.mark.parametrize("translate", [to_anthropic, to_openai])
def test_an_unknown_block_type_raises_rather_than_being_dropped(translate):
    """Silently dropping a block would send a truncated document to the model
    and there would be nothing in the response to reveal it."""
    with pytest.raises(ValueError, match="audio"):
        translate([{"type": "audio", "data": b""}])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_providers.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'renewal.providers'`

- [ ] **Step 3: Write the implementation**

`renewal/providers.py`:

```python
"""Model providers.

The extraction pipeline speaks one neutral content format. Each client
translates that format to its own wire format, so adding a provider never
touches the pipeline and no vendor's message shape leaks into the runner.
"""

from __future__ import annotations

import base64


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def image_block(png: bytes) -> dict:
    return {"type": "image_png", "data": png}


def to_anthropic(content: list[dict]) -> list[dict]:
    out = []
    for block in content:
        if block["type"] == "text":
            out.append({"type": "text", "text": block["text"]})
        elif block["type"] == "image_png":
            out.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(block["data"]).decode(),
                    },
                }
            )
        else:
            raise ValueError(f"unknown content block type: {block['type']}")
    return out


def to_openai(content: list[dict]) -> list[dict]:
    out = []
    for block in content:
        if block["type"] == "text":
            out.append({"type": "text", "text": block["text"]})
        elif block["type"] == "image_png":
            encoded = base64.b64encode(block["data"]).decode()
            out.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{encoded}"},
                }
            )
        else:
            raise ValueError(f"unknown content block type: {block['type']}")
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_providers.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add renewal/providers.py tests/test_providers.py
git commit -m "feat: neutral content IR with per-provider translation"
```

---

## Task 2: The two clients

**Files:**
- Modify: `renewal/providers.py`
- Test: `tests/test_providers.py`

**Interfaces:**
- Consumes: `to_anthropic`, `to_openai`, `text_block`, `image_block` (Task 1).
- Produces: `ModelClient` Protocol with `complete(*, model: str, system: str, content: list[dict]) -> str`; `AnthropicClient(api_key: str)`; `OpenAICompatClient(base_url: str, api_key: str, *, timeout: float = 300.0, transport=None)`.

`AnthropicClient` is moved here from `renewal/extract/runner.py`; it is not a new class. It keeps its behaviour and gains only the `to_anthropic` call.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_providers.py`:

```python
import httpx

from renewal.providers import OpenAICompatClient


def _stub(handler):
    return httpx.MockTransport(handler)


def _ok(payload="extracted json here"):
    def handler(request):
        handler.request = request
        return httpx.Response(
            200, json={"choices": [{"message": {"content": payload}}]}
        )

    return handler


def test_openai_client_posts_to_chat_completions_and_returns_the_text():
    handler = _ok()
    client = OpenAICompatClient(
        "http://localhost:11434/v1", "", transport=_stub(handler)
    )

    result = client.complete(
        model="qwen2.5:7b", system="be exact", content=[text_block("hello")]
    )

    assert result == "extracted json here"
    assert str(handler.request.url) == "http://localhost:11434/v1/chat/completions"


def test_openai_client_sends_temperature_zero_and_the_system_message():
    import json

    handler = _ok()
    client = OpenAICompatClient(
        "https://api.x.ai/v1", "key", transport=_stub(handler)
    )
    client.complete(model="grok", system="be exact", content=[text_block("hi")])

    body = json.loads(handler.request.content)
    assert body["temperature"] == 0
    assert body["model"] == "grok"
    assert body["messages"][0] == {"role": "system", "content": "be exact"}
    assert body["messages"][1]["content"] == [{"type": "text", "text": "hi"}]


def test_openai_client_sends_the_bearer_token_when_there_is_one():
    handler = _ok()
    client = OpenAICompatClient(
        "https://api.openai.com/v1", "sk-abc", transport=_stub(handler)
    )
    client.complete(model="gpt", system="s", content=[text_block("hi")])
    assert handler.request.headers["authorization"] == "Bearer sk-abc"


def test_openai_client_omits_the_auth_header_when_there_is_no_key():
    """Ollama needs no key, and some local servers reject an empty bearer."""
    handler = _ok()
    client = OpenAICompatClient(
        "http://localhost:11434/v1", "", transport=_stub(handler)
    )
    client.complete(model="qwen2.5:7b", system="s", content=[text_block("hi")])
    assert "authorization" not in handler.request.headers


def test_openai_client_translates_images_to_data_uris():
    import json

    handler = _ok()
    client = OpenAICompatClient("http://x/v1", "", transport=_stub(handler))
    client.complete(model="m", system="s", content=[image_block(PNG)])

    body = json.loads(handler.request.content)
    assert body["messages"][1]["content"][0]["type"] == "image_url"


def test_openai_client_raises_on_a_non_2xx_response():
    """A 401 must not be parsed as if it were a completion."""

    def handler(request):
        return httpx.Response(401, json={"error": "bad key"})

    client = OpenAICompatClient("http://x/v1", "nope", transport=_stub(handler))
    with pytest.raises(httpx.HTTPStatusError):
        client.complete(model="m", system="s", content=[text_block("hi")])


def test_a_trailing_slash_on_the_base_url_does_not_double_up():
    handler = _ok()
    client = OpenAICompatClient("http://x/v1/", "", transport=_stub(handler))
    client.complete(model="m", system="s", content=[text_block("hi")])
    assert str(handler.request.url) == "http://x/v1/chat/completions"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_providers.py -v`
Expected: FAIL — `ImportError: cannot import name 'OpenAICompatClient'`

- [ ] **Step 3: Write the implementation**

Add to the imports at the top of `renewal/providers.py`:

```python
from typing import Protocol

import httpx
```

Then append to `renewal/providers.py`:

```python
class ModelClient(Protocol):
    def complete(self, *, model: str, system: str, content: list[dict]) -> str: ...


class AnthropicClient:
    """Anthropic's native API. Moved here from renewal/extract/runner.py."""

    def __init__(self, api_key: str) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)

    def complete(self, *, model: str, system: str, content: list[dict]) -> str:
        message = self._client.messages.create(
            model=model,
            max_tokens=8192,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": to_anthropic(content)}],
        )
        return "".join(block.text for block in message.content if block.type == "text")


class OpenAICompatClient:
    """Any endpoint speaking OpenAI's /chat/completions.

    Written against httpx rather than the openai package deliberately: the call
    is a single POST, and third-party endpoints regularly reject parameters the
    official SDK sends by default. Sending exactly the fields every endpoint
    accepts is the more robust choice here.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    def complete(self, *, model: str, system: str, content: list[dict]) -> str:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": to_openai(content)},
            ],
        }
        with httpx.Client(
            timeout=self._timeout, transport=self._transport
        ) as http:
            response = http.post(
                f"{self.base_url}/chat/completions", headers=headers, json=payload
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_providers.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add renewal/providers.py tests/test_providers.py
git commit -m "feat: Anthropic and OpenAI-compatible model clients"
```

---

## Task 3: Registry, configuration, and packaging

**Files:**
- Modify: `renewal/providers.py`, `renewal/config.py`, `.env.example`, `pyproject.toml`
- Test: `tests/test_providers.py`

**Interfaces:**
- Consumes: `AnthropicClient`, `OpenAICompatClient` (Task 2); `Settings` (`renewal/config.py`).
- Produces: `PRESETS: dict[str, Preset]` where `Preset` has `base_url: str | None`; `build_client(settings: Settings) -> ModelClient`; `Settings.provider`, `Settings.llm_base_url`, `Settings.llm_api_key`; `renewal.config.PROVIDER_KEY_ENV: dict[str, str | None]`.

`PROVIDER_KEY_ENV` lives in `config.py`, not `providers.py`, so that `providers` can import `config` without a cycle.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_providers.py`:

```python
from renewal.config import Settings
from renewal.providers import AnthropicClient, build_client


def _settings(**overrides):
    base = dict(
        database_url="postgresql+psycopg:///renewal_test",
        blob_root="/tmp/blobs",
        anthropic_api_key="",
        extraction_model="m",
        draft_model="m",
        confidence_threshold=0.80,
        materiality_config="config/materiality.yaml",
    )
    base.update(overrides)
    return Settings(**base)


def test_settings_default_to_anthropic_so_existing_config_keeps_working():
    settings = _settings()
    assert settings.provider == "anthropic"
    assert settings.llm_base_url is None
    assert settings.llm_api_key == ""


def test_anthropic_provider_builds_the_native_client():
    client = build_client(_settings(provider="anthropic", anthropic_api_key="sk-x"))
    assert isinstance(client, AnthropicClient)


@pytest.mark.parametrize(
    "provider,expected_url",
    [
        ("openai", "https://api.openai.com/v1"),
        ("grok", "https://api.x.ai/v1"),
        ("ollama", "http://localhost:11434/v1"),
        ("huggingface", "https://router.huggingface.co/v1"),
    ],
)
def test_each_preset_resolves_to_its_base_url(provider, expected_url):
    client = build_client(_settings(provider=provider, llm_api_key="key"))
    assert client.base_url == expected_url


def test_ollama_needs_no_key():
    client = build_client(_settings(provider="ollama"))
    assert client.base_url == "http://localhost:11434/v1"


def test_llm_base_url_overrides_the_preset():
    client = build_client(
        _settings(provider="ollama", llm_base_url="http://gpu-box:8000/v1")
    )
    assert client.base_url == "http://gpu-box:8000/v1"


def test_custom_without_a_base_url_raises_at_construction():
    """Failing here means the app refuses to start, rather than failing on the
    first upload with two documents already ingested."""
    with pytest.raises(ValueError, match="LLM_BASE_URL"):
        build_client(_settings(provider="custom", llm_api_key="k"))


def test_a_provider_whose_key_is_missing_raises_at_construction():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        build_client(_settings(provider="openai"))


def test_anthropic_without_a_key_raises_at_construction():
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        build_client(_settings(provider="anthropic"))


def test_an_unknown_provider_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="ollama"):
        build_client(_settings(provider="not-a-provider"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_providers.py -v`
Expected: FAIL — `TypeError: Settings.__init__() got an unexpected keyword argument 'provider'`

- [ ] **Step 3: Extend the settings**

In `renewal/config.py`, add above the `Settings` class:

```python
PROVIDER_KEY_ENV: dict[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "grok": "XAI_API_KEY",
    "ollama": None,
    "huggingface": "HF_TOKEN",
    "custom": "LLM_API_KEY",
}
```

Add these three fields to the end of the `Settings` dataclass. They must come
last, because a dataclass field with a default cannot precede one without:

```python
    provider: str = "anthropic"
    llm_base_url: str | None = None
    llm_api_key: str = ""
```

In `load_settings()`, add before the `return`:

```python
    provider = os.environ.get("PROVIDER", "anthropic")
    key_env = PROVIDER_KEY_ENV.get(provider)
```

and add these three arguments to the `Settings(...)` call:

```python
        provider=provider,
        llm_base_url=os.environ.get("LLM_BASE_URL") or None,
        llm_api_key=os.environ.get(key_env, "") if key_env else "",
```

- [ ] **Step 4: Write the registry**

Add to the imports at the top of `renewal/providers.py`:

```python
from dataclasses import dataclass

from renewal.config import PROVIDER_KEY_ENV, Settings
```

Append to `renewal/providers.py`:

```python
@dataclass(frozen=True)
class Preset:
    base_url: str | None


PRESETS: dict[str, Preset] = {
    "anthropic": Preset(base_url=None),
    "openai": Preset(base_url="https://api.openai.com/v1"),
    "grok": Preset(base_url="https://api.x.ai/v1"),
    "ollama": Preset(base_url="http://localhost:11434/v1"),
    "huggingface": Preset(base_url="https://router.huggingface.co/v1"),
    "custom": Preset(base_url=None),
}


def build_client(settings: Settings) -> ModelClient:
    """The only place a model client is constructed.

    Raises rather than returning a client that cannot work, so a missing key is
    a startup failure instead of a failure on the first upload.
    """
    if settings.provider not in PRESETS:
        known = ", ".join(sorted(PRESETS))
        raise ValueError(f"unknown provider {settings.provider!r}; known: {known}")

    if settings.provider == "anthropic":
        if not settings.anthropic_api_key:
            raise ValueError("provider 'anthropic' needs ANTHROPIC_API_KEY")
        return AnthropicClient(settings.anthropic_api_key)

    base_url = settings.llm_base_url or PRESETS[settings.provider].base_url
    if not base_url:
        raise ValueError(f"provider {settings.provider!r} needs LLM_BASE_URL")

    key_env = PROVIDER_KEY_ENV.get(settings.provider)
    if key_env and not settings.llm_api_key:
        raise ValueError(f"provider {settings.provider!r} needs {key_env}")

    return OpenAICompatClient(base_url, settings.llm_api_key)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_providers.py -v`
Expected: 25 passed

- [ ] **Step 6: Promote httpx to a runtime dependency**

`httpx` is currently installed only as a transitive dependency of the
`anthropic` SDK and declared only under `dev`. `providers.py` imports it at
module level, so it must be declared for real.

In `pyproject.toml`, add `"httpx>=0.27",` to the `dependencies` list, and
change the dev extra to:

```toml
dev = ["pytest>=8.0"]
```

Then: `.venv/bin/pip install -q -e ".[dev]"`

- [ ] **Step 7: Document the providers**

Replace `.env.example` with:

```
# Which model provider to use. One of:
#   anthropic  openai  grok  ollama  huggingface  custom
PROVIDER=anthropic

DATABASE_URL=postgresql+psycopg:///renewal
TEST_DATABASE_URL=postgresql+psycopg:///renewal_test
BLOB_ROOT=blobs

# Model names as the selected provider spells them.
EXTRACTION_MODEL=claude-opus-5
DRAFT_MODEL=claude-sonnet-5

CONFIDENCE_THRESHOLD=0.80
MATERIALITY_CONFIG=config/materiality.yaml

# One key, for whichever provider is selected. Ollama needs none.
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
XAI_API_KEY=
HF_TOKEN=
LLM_API_KEY=

# Only for PROVIDER=custom, or to point a preset somewhere else.
# vLLM, LM Studio, Text Generation Inference and llama.cpp all serve this.
LLM_BASE_URL=

# Running locally with Ollama:
#   ollama pull qwen2.5:7b
#   PROVIDER=ollama
#   EXTRACTION_MODEL=qwen2.5:7b
#   DRAFT_MODEL=qwen2.5:7b
```

- [ ] **Step 8: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all passing. Nothing else consumes the new settings yet.

- [ ] **Step 9: Commit**

```bash
git add renewal/providers.py renewal/config.py tests/test_providers.py \
        pyproject.toml .env.example
git commit -m "feat: provider registry selected by one PROVIDER setting"
```

---

## Task 4: Move the runner onto the neutral IR

**Files:**
- Modify: `renewal/extract/runner.py`, `renewal/app.py`, `evals/test_extraction.py`, `scripts/compare_versions.py`, `scripts/reextract.py`
- Test: `tests/test_extract_scanned.py`, `tests/test_extract_runner.py`

**Interfaces:**
- Consumes: `text_block`, `image_block`, `build_client` (Tasks 1–3).
- Produces: `renewal.extract.runner` no longer defines `AnthropicClient`; `extract()` is unchanged in signature; `extraction.model_id` now holds `f"{settings.provider}:{settings.extraction_model}"`.

**Two existing tests change deliberately.** Both are recorded here rather than
discovered mid-task, because a changed test is a changed guarantee:

1. `tests/test_extract_scanned.py` asserts Anthropic wire format. Under the IR
   that tests the wrong layer; it is rewritten to assert the IR. The wire
   format it used to cover is covered properly by Task 1.
2. `tests/test_extract_runner.py:87` asserts `model_id == "claude-opus-5"` and
   becomes `"anthropic:claude-opus-5"`.

- [ ] **Step 1: Update the two existing tests**

In `tests/test_extract_runner.py`, change the assertion on line 87 to:

```python
    assert extraction.model_id == "anthropic:claude-opus-5"
```

In `tests/test_extract_scanned.py`, replace `test_scanned_document_is_sent_as_page_images` with:

```python
def test_scanned_document_is_sent_as_page_images(session, store, settings):
    document = ingest_pdf(
        session,
        store,
        data=make_scanned_pdf([LINES, LINES]),
        original_filename="scan.pdf",
    )
    client = CapturingClient(json.dumps({"fields": []}))

    extract(session, store, document, "v1", client=client, settings=settings)

    content = client.calls[0]
    assert content[0]["type"] == "text"
    images = [block for block in content if block["type"] == "image_png"]
    assert len(images) == 2
    assert images[0]["data"].startswith(b"\x89PNG")
```

The `import base64` at the top of that file becomes unused; remove it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_extract_scanned.py tests/test_extract_runner.py -v`
Expected: FAIL — the scanned test finds no `image_png` blocks (`assert 0 == 2`), and the runner test sees `"claude-opus-5"` where it wants `"anthropic:claude-opus-5"`.

- [ ] **Step 3: Rewrite the runner's content builder**

In `renewal/extract/runner.py`, delete the `import base64` line, delete the
`from typing import Protocol` line, delete the whole `ModelClient` Protocol
class, and delete the whole `AnthropicClient` class. Add to the imports:

```python
from renewal.providers import ModelClient, image_block, text_block
```

Replace `_build_content` with:

```python
def _build_content(prompt, data: bytes, pdf: PdfInfo, has_text_layer: bool) -> list[dict]:
    """Neutral content IR. Wire format belongs to the client, not here."""
    if has_text_layer:
        return [
            text_block(prompt.USER_TEXT_TEMPLATE.format(document_text=layout_text(pdf)))
        ]
    blocks = [text_block(prompt.USER_IMAGE_INSTRUCTION)]
    blocks.extend(image_block(png) for png in rasterize(data))
    return blocks
```

- [ ] **Step 4: Record the provider on the extraction**

In `renewal/extract/runner.py`, inside `_record`, change:

```python
            model_id=settings.extraction_model,
```

to:

```python
            model_id=f"{settings.provider}:{settings.extraction_model}",
```

- [ ] **Step 5: Update the four modules that imported AnthropicClient**

In `renewal/app.py`, replace the whole file with:

```python
from renewal.blobstore import BlobStore
from renewal.config import load_settings
from renewal.db import get_engine
from renewal.providers import build_client
from renewal.web import create_app
from sqlalchemy.orm import sessionmaker

_settings = load_settings()
app = create_app(
    settings=_settings,
    store=BlobStore(_settings.blob_root),
    model_client=build_client(_settings),
    session_factory=sessionmaker(bind=get_engine()),
)
```

In `evals/test_extraction.py`, change:

```python
from renewal.extract.runner import AnthropicClient, extract
```

to:

```python
from renewal.extract.runner import extract
from renewal.providers import build_client
```

and change:

```python
    client = AnthropicClient(settings.anthropic_api_key)
```

to:

```python
    client = build_client(settings)
```

In `scripts/compare_versions.py`, change:

```python
from renewal.extract.runner import AnthropicClient, extract
```

to:

```python
from renewal.extract.runner import extract
from renewal.providers import build_client
```

and change:

```python
    client = AnthropicClient(settings.anthropic_api_key)
```

to:

```python
    client = build_client(settings)
```

In `scripts/reextract.py`, make the identical pair of changes. Its import line:

```python
from renewal.extract.runner import AnthropicClient, extract
```

becomes:

```python
from renewal.extract.runner import extract
from renewal.providers import build_client
```

and:

```python
    client = AnthropicClient(settings.anthropic_api_key)
```

becomes:

```python
    client = build_client(settings)
```

`evals/test_extraction.py` is imported at collection time even though it is
deselected, so a stale import here fails the whole run, not just the eval.

`scripts/` is NOT on `testpaths`, so a stale import in either script leaves the
suite green while the script is broken. Grep to confirm you have them all
before you commit:

```bash
grep -rn 'AnthropicClient' --include=*.py . | grep -v '/.venv/'
```

The only hits left should be in `renewal/providers.py` and
`tests/test_providers.py`.

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all passing.

- [ ] **Step 7: Commit**

```bash
git add renewal/extract/runner.py renewal/app.py evals/test_extraction.py \
        scripts/compare_versions.py scripts/reextract.py \
        tests/test_extract_scanned.py tests/test_extract_runner.py
git commit -m "refactor: runner emits neutral content IR and records the provider"
```

---

## Task 5: Verification rate

**Files:**
- Modify: `renewal/extract/validate.py`, `renewal/web.py`, `renewal/templates/run_review.html`
- Test: `tests/test_extract_validate.py`, `tests/test_web_review.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `verification_rate(fields) -> float | None` in `renewal/extract/validate.py`; the review template context gains `rates: dict[int, float | None]` keyed by extraction id.

This is the only thing added to `validate.py`. The gate itself — `_check`,
`_normalize`, `validate_fields` — is not edited.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_extract_validate.py`:

```python
from dataclasses import dataclass

from renewal.extract.validate import verification_rate


@dataclass
class Row:
    validation_error: str | None


def test_rate_is_one_when_every_field_was_verified():
    assert verification_rate([Row(None), Row(None)]) == 1.0


def test_rate_counts_only_fields_whose_source_text_was_found():
    rows = [Row(None), Row("source_text not found on cited page"), Row(None), Row("x")]
    assert verification_rate(rows) == 0.5


def test_rate_is_zero_when_nothing_could_be_verified():
    """The honest reading of a scanned page, and of a model that paraphrases."""
    assert verification_rate([Row("source_text not found on cited page")]) == 0.0


def test_rate_is_none_rather_than_a_division_by_zero_for_an_empty_extraction():
    assert verification_rate([]) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_extract_validate.py -v`
Expected: FAIL — `ImportError: cannot import name 'verification_rate'`

- [ ] **Step 3: Write the implementation**

Append to `renewal/extract/validate.py`:

```python
def verification_rate(fields) -> float | None:
    """Share of returned fields whose source text was found on the cited page.

    Computed on read, never stored: a stored copy could disagree with the rows
    it summarises. `None` for an extraction that returned no fields at all,
    which is a different fact from a rate of zero.

    Accepts anything carrying `validation_error` — both `ValidatedField` and the
    persisted `ExtractedField`.
    """
    fields = list(fields)
    if not fields:
        return None
    verified = sum(1 for field in fields if field.validation_error is None)
    return verified / len(fields)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_extract_validate.py -v`
Expected: all passing.

- [ ] **Step 5: Write the failing web test**

Append to `tests/test_web_review.py`:

```python
def test_review_screen_shows_the_verification_rate(app, seeded):
    """That file's stub returns the same response for both documents, citing
    the prior page's premium. So the prior side verifies and the renewal side
    cannot: one screen shows both ends of the scale."""
    _, policy_id = seeded
    with TestClient(app) as client:
        location = _upload(client, policy_id).headers["location"]
        page = client.get(location)
    assert "100% verified" in page.text  # prior: the quote is on the page
    assert "0% verified" in page.text  # renewal: the quote is not
```

`app`, `seeded` and `_upload` already exist in that file; do not add fixtures.
Do not change `FakeClient` or `_response` to make both sides verify — the
asymmetry is what makes this test worth having.

- [ ] **Step 6: Run it to verify it fails**

Run: `.venv/bin/pytest tests/test_web_review.py -v`
Expected: FAIL — `assert 'verified' in ...`

- [ ] **Step 7: Put the rate into the review context**

In `renewal/web.py`, add to the imports:

```python
from renewal.extract.validate import verification_rate
```

In the `review` route, change:

```python
            sides, extra_fields, blocked = [], {}, []
```

to:

```python
            sides, extra_fields, blocked, rates = [], {}, [], {}
```

Add immediately after the `blocked.extend(...)` line:

```python
                rates[extraction.id] = verification_rate(fields)
```

and add to the template context dict:

```python
                    "rates": rates,
```

- [ ] **Step 8: Show it in the template**

In `renewal/templates/run_review.html`, replace the extraction header paragraph:

```html
    <p class="source">
      extraction #{{ extraction.id }} · {{ extraction.extractor_version }} ·
      {{ extraction.model_id }} · status {{ extraction.status }}
    </p>
```

with:

```html
    <p class="source">
      extraction #{{ extraction.id }} · {{ extraction.extractor_version }} ·
      {{ extraction.model_id }} · status {{ extraction.status }} ·
      {% if rates[extraction.id] is none %}
        no fields returned
      {% else %}
        <span {% if rates[extraction.id] < 0.5 %}class="flag"{% endif %}>
          {{ "%.0f"|format(100 * rates[extraction.id]) }}% verified
        </span>
      {% endif %}
    </p>
```

A model that cannot quote the document shows a red low percentage on its first
run and disqualifies itself, which is what makes leaving the gate alone
workable.

- [ ] **Step 9: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all passing.

- [ ] **Step 10: Commit**

```bash
git add renewal/extract/validate.py renewal/web.py \
        renewal/templates/run_review.html tests/test_extract_validate.py \
        tests/test_web_review.py
git commit -m "feat: verification rate exposes models that cannot quote the document"
```

---

## Task 6: Per-provider eval baselines

**Files:**
- Modify: `evals/accuracy.py`, `evals/test_extraction.py`, `scripts/compare_versions.py`
- Delete: `evals/baseline.json`
- Create: `evals/baselines/.gitkeep`
- Test: `tests/test_accuracy.py`

**Interfaces:**
- Consumes: `verification_rate` (Task 5), `build_client` (Task 3).
- Produces: `baseline_path(directory: Path, provider: str, model: str, version: str) -> Path` in `evals/accuracy.py`.

`evals/baseline.json` currently holds `{}`, so there is nothing to migrate.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_accuracy.py`:

```python
from pathlib import Path

from evals.accuracy import baseline_path


def test_baseline_path_starts_with_a_readable_slug():
    path = baseline_path(Path("evals/baselines"), "anthropic", "claude-opus-5", "v1")
    assert path.name.startswith("anthropic__claude-opus-5__v1-")
    assert path.suffix == ".json"


def test_characters_that_are_illegal_in_a_filename_are_replaced():
    """Ollama model names carry a colon; HuggingFace repo ids carry a slash."""
    assert baseline_path(Path("b"), "ollama", "qwen2.5:7b", "v1").name.startswith(
        "ollama__qwen2.5-7b__v1-"
    )
    assert baseline_path(
        Path("b"), "huggingface", "meta-llama/Llama-3.1-8B", "v1"
    ).name.startswith("huggingface__meta-llama-Llama-3.1-8B__v1-")


def test_the_path_is_stable_across_calls():
    args = (Path("b"), "ollama", "qwen2.5:7b", "v1")
    assert baseline_path(*args) == baseline_path(*args)


def test_models_differing_only_by_an_illegal_character_do_not_collide():
    """The readable slug alone maps both of these to "ollama__qwen2.5-7b__v1".
    A collision here would silently gate one model's run against another
    model's recorded baseline."""
    assert (
        baseline_path(Path("b"), "ollama", "qwen2.5:7b", "v1")
        != baseline_path(Path("b"), "ollama", "qwen2.5-7b", "v1")
    )


def test_the_separator_cannot_be_forged_out_of_a_provider_or_model_name():
    """"_" survives sanitisation, so slug text alone is ambiguous about where
    the provider ends and the model begins."""
    assert (
        baseline_path(Path("b"), "a_", "b", "v1")
        != baseline_path(Path("b"), "a", "_b", "v1")
    )


def test_two_different_models_never_share_a_baseline_file():
    a = baseline_path(Path("b"), "ollama", "qwen2.5:7b", "v1")
    b = baseline_path(Path("b"), "anthropic", "claude-opus-5", "v1")
    assert a != b
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_accuracy.py -v`
Expected: FAIL — `ImportError: cannot import name 'baseline_path'`

- [ ] **Step 3: Write the implementation**

Add `import hashlib` and `import re` to the imports in `evals/accuracy.py`,
then append:

```python
def baseline_path(directory: Path, provider: str, model: str, version: str) -> Path:
    """One baseline per provider, model, and extractor version.

    A single baseline keyed by fixture alone would compare one model's results
    against another's — either failing spuriously or, worse, passing silently
    over a real regression.

    The readable slug is for humans and is not unique on its own: collapsing
    every illegal character onto "-" maps "qwen2.5:7b" and "qwen2.5-7b" to the
    same name, and "_" surviving means provider "a_" with model "b" collides
    with provider "a" and model "_b". The digest of the raw triple is what
    actually keeps two models apart.
    """
    digest = hashlib.sha256(
        "\x00".join((provider, model, version)).encode()
    ).hexdigest()[:8]
    slug = re.sub(r"[^A-Za-z0-9._-]", "-", f"{provider}__{model}__{version}")
    return Path(directory) / f"{slug}-{digest}.json"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_accuracy.py -v`
Expected: all passing.

- [ ] **Step 5: Point the harness at the per-model baseline**

In `evals/test_extraction.py`, replace:

```python
BASELINE = Path(__file__).parent / "baseline.json"
```

with:

```python
BASELINE_DIR = Path(__file__).parent / "baselines"
```

and change the import line to bring in the helper and the rate:

```python
from evals.accuracy import (
    accuracy,
    baseline_path,
    load_fixtures,
    regressions,
    report,
    score,
)
from renewal.extract.validate import verification_rate
```

Inside the test, after `settings = load_settings()`, add:

```python
    baseline = baseline_path(
        BASELINE_DIR, settings.provider, settings.extraction_model, VERSION
    )
```

Record the verification rate alongside the score by replacing the
`results[fixture.fixture_id] = {...}` assignment with:

```python
        results[fixture.fixture_id] = {
            "carrier": fixture.carrier,
            "fields": score(fixture.fields, actual),
            "verification_rate": verification_rate(extraction.fields),
        }
```

Replace the print loop with one that shows both numbers:

```python
    with capsys.disabled():
        print(f"\nprovider: {settings.provider}  model: {settings.extraction_model}")
        print(report(results))
        for fixture_id, entry in sorted(results.items()):
            rate = entry["verification_rate"]
            shown = "n/a" if rate is None else f"{100 * rate:5.1f}%"
            print(
                f"  {fixture_id:<28} {100 * accuracy(entry['fields']):5.1f}% "
                f"verified {shown}"
            )
```

Replace the baseline block at the end with:

```python
    if baseline.exists():
        recorded = json.loads(baseline.read_text())
        broken = regressions(
            {k: v["fields"] for k, v in recorded.items()},
            {k: v["fields"] for k, v in results.items()},
        )
        assert not broken, f"fields that used to pass and now fail: {broken}"

    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.with_suffix(".latest.json").write_text(json.dumps(results, indent=2))
```

- [ ] **Step 6: Turn compare_versions into a baseline diff**

`scripts/compare_versions.py` currently re-runs the extractor twice and makes
real API calls. Comparing two recorded baselines instead means the comparison
is free, repeatable, and works across providers — which is the model bake-off.

Replace the whole file with:

```python
"""Per-field accuracy diff between two recorded baselines.

Usage: python scripts/compare_versions.py <baseline-a.json> <baseline-b.json>

Reads files only — no API calls. The two baselines may differ in extractor
version, provider, model, or all three, which is what makes this a model
comparison as well as a version comparison.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def field_results(path: Path) -> dict[str, dict[str, bool]]:
    data = json.loads(Path(path).read_text())
    return {fixture: entry["fields"] for fixture, entry in data.items()}


def accuracy_by_path(results: dict[str, dict[str, bool]]) -> dict[str, float]:
    totals: dict[str, list[bool]] = {}
    for fields in results.values():
        for field_path, passed in fields.items():
            totals.setdefault(field_path, []).append(passed)
    return {
        field_path: 100 * sum(values) / len(values)
        for field_path, values in totals.items()
    }


def main(path_a: str, path_b: str) -> None:
    a = accuracy_by_path(field_results(Path(path_a)))
    b = accuracy_by_path(field_results(Path(path_b)))
    label_a, label_b = Path(path_a).stem, Path(path_b).stem

    print(f"{'field path':<44} {label_a:>24} {label_b:>24}   delta")
    for field_path in sorted(set(a) | set(b)):
        a_pct, b_pct = a.get(field_path, 0.0), b.get(field_path, 0.0)
        if a_pct != b_pct:
            print(
                f"{field_path:<44} {a_pct:23.1f}% {b_pct:23.1f}% "
                f" {b_pct - a_pct:+6.1f}"
            )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

- [ ] **Step 7: Remove the old baseline and keep the directory**

```bash
git rm evals/baseline.json
mkdir -p evals/baselines
touch evals/baselines/.gitkeep
```

- [ ] **Step 8: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all passing. `evals/test_extraction.py` must still import cleanly at
collection time even though it is deselected.

- [ ] **Step 9: Commit**

```bash
git add evals scripts/compare_versions.py tests/test_accuracy.py
git commit -m "feat: eval baselines keyed by provider, model, and version"
```

---

## Task 7: Privacy note, and prove it against a local model

**Files:**
- Modify: `PRIVACY.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Rewrite the privacy note**

`PRIVACY.md` currently states flatly that documents are sent to Anthropic. Under
`PROVIDER=ollama` that is false, and a privacy note that is false in one of its
supported configurations is worse than no note.

Replace the section titled `## Documents are sent to a third-party model API`
(and only that section — leave the rest of the file alone) with:

```markdown
## Where documents go depends on PROVIDER

This tool sends declarations pages to a model for extraction and for drafting
the client explanation. Documents with a text layer are sent as text; scanned
documents are sent as page images. **Which model, and therefore whether the
document leaves this machine, is set by `PROVIDER` in `.env`.**

Third-party providers — the document is transmitted to a company outside your
control, subject to that company's terms and retention policy:

| `PROVIDER` | Documents are sent to |
|---|---|
| `anthropic` | Anthropic |
| `openai` | OpenAI |
| `grok` | xAI |
| `huggingface` | HuggingFace, and the inference provider it routes to |

Local providers — nothing leaves the host:

| `PROVIDER` | Documents are sent to |
|---|---|
| `ollama` | a model running on this machine |
| `custom` | whatever `LLM_BASE_URL` points at — local if that is a local address, third-party if it is not |

`custom` is only as private as the address configured, so read `LLM_BASE_URL`
before answering the question for a running install.

To answer this for a specific install, read `PROVIDER` in its `.env`. Every
extraction also records the provider and model it used in
`extraction.model_id`, so the question can be answered retrospectively for any
document already processed.
```

- [ ] **Step 2: Check the rest of the file still reads true**

Read `PRIVACY.md` start to finish. The sections on storage, retention, logging,
and version control are unaffected by this work and must not be edited. Confirm
no other sentence in the file asserts that documents go to Anthropic.

- [ ] **Step 3: Run the whole suite**

Run: `.venv/bin/pytest -q`
Expected: all passing.

- [ ] **Step 4: Commit**

```bash
git add PRIVACY.md
git commit -m "docs: privacy note covers local and third-party providers"
```

- [ ] **Step 5: Prove it end to end against a local model**

This step needs a real dec page and cannot be done from the test suite.

```bash
ollama pull qwen2.5vl:7b     # a vision model; dec pages are often scanned
ollama serve                 # if it is not already running
```

In `.env`: `PROVIDER=ollama`, `EXTRACTION_MODEL=qwen2.5vl:7b`,
`DRAFT_MODEL=qwen2.5vl:7b`. Restart the app, upload a real prior and renewal
dec page, and read the review screen.

Record two things: the **verification rate** each extraction reports, and how
many fields are flagged `needs_review`. A low verification rate means the model
is not quoting the document, and the honest response is to try a larger model
or go back to a frontier provider — not to touch `validate.py`.

Then run the eval harness under both providers and diff them:

```bash
pytest -m eval evals/test_extraction.py -s          # once per PROVIDER
cp evals/baselines/<name>.latest.json evals/baselines/<name>.json
python scripts/compare_versions.py \
    evals/baselines/anthropic__claude-opus-5__v1.json \
    evals/baselines/ollama__qwen2.5vl-7b__v1.json
```

That diff is the answer to "is a local model good enough for this", and it is
the reason the baselines are keyed the way they are.

---

## Done

Extraction and drafting run against Anthropic, OpenAI, Grok, HuggingFace, or any
local OpenAI-compatible server, chosen by one setting. The corpus records which
provider and model produced every extraction. The verbatim source-text gate is
exactly where it was, and a verification rate now makes it obvious within one
run when a model cannot clear it.
