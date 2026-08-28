# Multi-Provider Model Support — Design

**Date:** 2026-08-28
**Status:** Approved for implementation planning
**Author:** brainstormed with Claude Code
**Supersedes nothing.** Extends `2026-08-27-renewal-comparison-tool-design.md`.

---

## 1. Context

v0 shipped against one model provider. `renewal/extract/runner.py` builds Anthropic
message blocks inline and `renewal/app.py` constructs an `AnthropicClient` directly, so
the provider is welded into the extraction path even though the pipeline already accepts
a client by injection.

Four things motivate widening it, and all four were named as reasons:

1. **Privacy.** These are real declarations pages carrying names, addresses, VINs, and
   sometimes dates of birth. Every document currently leaves the machine. Local
   inference removes that, which changes the answer the agency gets when it asks where
   its clients' documents go.
2. **Cost.** Frontier extraction on every document, re-run over the whole corpus every
   time the extractor changes, is the dominant recurring cost of the design.
3. **Flexibility.** Deciding which model is actually best for a dec page requires running
   several against the same labelled fixtures and reading the per-field result.
4. **Portability.** A design-partner agency should be able to run this without holding
   the author's API key.

Privacy leads. Local inference is therefore a first-class path, not a fallback.

## 2. Non-negotiable constraints

The v0 constraints all still hold. Three deserve restating because this work presses
directly on them.

1. **The source-text gate does not move.** Every extracted field must quote the document
   verbatim, `validate_fields` checks that quote against the cited page, and an
   unverifiable field is kept at confidence 0.0 and blocks promotion. Weaker models will
   fail this often. That is the correct outcome and it is not to be softened — not by a
   per-provider threshold, not by fuzzy matching, not by a flag.
2. **Nothing is sent automatically.** Unchanged. A different model behind the draft does
   not change what a draft is.
3. **Every table stays insert-only.** No migration is introduced by this work.

One constraint is added:

4. **The provider must never be inferable from the corpus by guesswork.** Which provider
   and model produced an extraction is recorded on the row, because a corpus that mixes
   models without saying so cannot be used to evaluate either one.

## 3. Scope

**In scope.** A provider abstraction with two implementations; a provider registry keyed
by a single `PROVIDER` setting; a documented turnkey local path; provider and model
recorded on every extraction; a verification-rate signal surfaced in the UI and the eval
report; per-provider-and-model eval baselines; a rewritten privacy note.

**Explicitly out of scope.** Do not build without asking first: streaming responses;
retry and backoff policy beyond the HTTP client's defaults; token counting or cost
tracking; separate providers for extraction and drafting; automatic model downloading or
runtime supervision; per-provider structured-output or JSON modes; a model-picker in the
web UI.

## 4. Provider abstraction

### 4.1 The neutral content IR

`runner.py` stops emitting one provider's wire format. It builds a neutral intermediate
representation, and each client translates that to its own wire format:

```python
[
  {"type": "text", "text": "..."},
  {"type": "image_png", "data": b"\x89PNG..."},
]
```

Only two block types exist, because the extraction path only ever sends prompt text and
rasterized pages. `image_png` carries raw bytes; base64 encoding is a wire concern and
belongs to whichever client needs it.

Anthropic renders an `image_png` block to:

```python
{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}
```

OpenAI-compatible endpoints render it to:

```python
{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
```

This is the one interface change in the work. It puts wire format inside the client,
where a new provider can be added without touching the extraction pipeline, and it makes
`runner.py` provider-agnostic in fact rather than by convention.

### 4.2 The protocol

The protocol is unchanged from v0:

```python
class ModelClient(Protocol):
    def complete(self, *, model: str, system: str, content: list[dict]) -> str: ...
```

**The provider name is read from `Settings`, not from the client.** Putting a `provider`
attribute on the protocol reads better in isolation, but every test in the suite passes a
hand-written stub client, and requiring an attribute of them would break tests this work
has no business touching. `settings.provider` is also the honest source: it is what
`build_client` dispatched on, so it cannot disagree with the client that was built.

### 4.3 The two implementations

Both live in a new `renewal/providers.py`. `AnthropicClient` moves there unchanged apart
from IR translation.

**`AnthropicClient`** wraps the `anthropic` SDK, as today.

**`OpenAICompatClient`** posts to `{base_url}/chat/completions` and differs between
services only by `base_url` and API key. It is written against `httpx` directly rather
than the `openai` package: the request is a single POST, `httpx` is already installed as
a dependency of the `anthropic` SDK, and third-party OpenAI-compatible endpoints
regularly reject parameters that the official SDK sends by default. A forty-line client
that sends exactly the fields every endpoint accepts is more robust here than a library
tracking one vendor's API surface.

Both send `temperature=0`, as v0 requires.

## 5. Provider registry and configuration

A single `PROVIDER` setting selects the client. Presets supply a base URL and name the
environment variable holding the key; they are defaults over one class, not subclasses.

| `PROVIDER` | Base URL | API key variable |
|---|---|---|
| `anthropic` | native SDK | `ANTHROPIC_API_KEY` |
| `openai` | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| `grok` | `https://api.x.ai/v1` | `XAI_API_KEY` |
| `ollama` | `http://localhost:11434/v1` | none |
| `huggingface` | `https://router.huggingface.co/v1` | `HF_TOKEN` |
| `custom` | `LLM_BASE_URL` | `LLM_API_KEY` |

`custom` is the escape hatch, and it is how vLLM, LM Studio, Text Generation Inference,
and `llama.cpp`'s server are reached. Every one of them serves an OpenAI-compatible
`/v1/chat/completions`, so they need no code of their own.

`Settings` gains `provider`, `llm_base_url`, and `llm_api_key`, **all with defaults**
(`provider` defaults to `anthropic`) so that the many tests constructing `Settings`
directly keep working unchanged. `extraction_model` and `draft_model` keep their meaning and now hold whatever the selected provider
calls its models. `build_client(settings) -> ModelClient` resolves the registry and is
the only place a client is constructed; `renewal/app.py` and the eval harness both call
it.

**One provider governs both extraction and drafting.** The draft prompt carries extracted
field values, so routing drafts to a third party while extracting locally would leak the
data the local path exists to protect. Splitting the setting per role is a two-line
change if a reason for it ever appears.

**A missing key is a startup failure, not a runtime one.** `build_client` raises when the
selected provider needs a key and none is set, so the app refuses to start rather than
failing on the first upload. `ollama` needs no key.

## 6. The local path

Ollama is the documented happy path because it installs as one binary, serves an
OpenAI-compatible API on `localhost:11434` with no key, and pulls models by name:

```bash
ollama pull qwen2.5:7b
# .env
PROVIDER=ollama
EXTRACTION_MODEL=qwen2.5:7b
DRAFT_MODEL=qwen2.5:7b
```

Nothing else is blessed and nothing is managed. The application does not download
models, spawn servers, or health-check a runtime it did not start — that belongs to a
repository willing to own subprocess lifecycles, and this one has already refused Docker,
CI, and job queues for the same reason.

**Vision on the scanned path is a property of the chosen model, not of the provider.** A
text-only local model sent page images will return nothing usable. The verification rate
in §7.2 makes that visible immediately rather than silently.

## 7. What this records in the corpus

### 7.1 Provider and model on every extraction

`extraction.model_id` starts recording `"<provider>:<model>"` — `anthropic:claude-opus-5`,
`ollama:qwen2.5:7b`. The column is already a string and the table is insert-only, so
**no migration is required**. Rows written before this change keep their bare model names
and are read as provider-unknown, which is accurate: they were all Anthropic, but the row
does not say so and will not be rewritten to claim otherwise.

### 7.2 Verification rate

The share of returned fields whose `source_text` was found on the cited page —
`fields with validation_error IS NULL ÷ total fields` — is **computed on read**, never
stored. It needs no column and no migration, and a stored copy could disagree with the
rows it summarizes.

It appears per extraction on the review screen and per model in the eval report. This is
the instrument that replaces softening the gate: a model that cannot quote the document
shows a near-zero verification rate on its first run and disqualifies itself, visibly,
without anyone having to weaken a check to discover it.

## 8. Eval harness

The harness currently hardcodes `AnthropicClient` and keys `baseline.json` by fixture id
alone. Running a second model against it would compare that model's results to a baseline
recorded from a different one — producing either spurious regression failures or, worse,
a silent pass over a real regression.

**Baselines become one file per provider, model, and extractor version:**

```
evals/baselines/<provider>__<model>__<extractor_version>.json
```

with `/` and `:` in model names replaced by `-` so the name is a legal filename. The
regression gate only ever compares like with like. Each file stays small and diffs
readably. The existing `evals/baseline.json` holds `{}`, so there is nothing to migrate;
it is removed.

`evals/test_extraction.py` builds its client through `build_client` and writes to the
baseline path for whatever provider and model it ran under. `scripts/compare_versions.py`
generalizes from "two extractor versions" to "any two baseline files", which is what
makes a model bake-off a command rather than a manual read of printed output.

The eval report gains a verification-rate column alongside per-field accuracy, because a
model can be accurate on the fields it returns while failing to quote any of them.

## 9. Privacy

`PRIVACY.md` currently states plainly that documents are sent to a third-party model API.
Under `PROVIDER=ollama` that sentence is false, and a privacy note that is false in one
of its supported configurations is worse than none.

It is rewritten to describe both modes explicitly: which providers are third-party and
what leaves the machine under each, that the local path sends nothing off the host, and
that the active mode is determined by `PROVIDER` in `.env`. It states where to read the
answer for a running install rather than asserting one configuration as fact.

## 10. Testing

TDD throughout, as in v0. No test in the default run makes a network call.

- **Provider translation.** For each client, the neutral IR renders to that provider's
  wire format: text-only content, and content carrying page images. These are pure
  functions over the IR and need no transport.
- **Transport.** `OpenAICompatClient` is exercised against a stubbed `httpx` transport:
  correct URL, correct auth header, `temperature=0` sent, assistant text returned, and a
  non-2xx response raising rather than being parsed.
- **Registry.** Each `PROVIDER` value resolves to the expected client and base URL; a
  provider whose key is missing raises at construction; `custom` without `LLM_BASE_URL`
  raises.
- **Runner.** `model_id` records `provider:model`. The runner emits neutral IR and never
  a provider's wire format.
- **Verification rate.** Computed correctly over mixed verified and unverified fields,
  and over an extraction with no fields at all, which must not divide by zero.

**One existing test is deliberately rewritten.** `tests/test_extract_scanned.py` asserts
Anthropic wire format directly:

```python
assert images[0]["source"]["media_type"] == "image/png"
```

Under the neutral IR that assertion tests the wrong layer. It is rewritten to assert the
IR — that the runner emits one `image_png` block per page, carrying PNG bytes — and the
wire format it used to cover is picked up properly by the per-provider translation tests.
This is recorded here rather than done quietly, because a changed test is a changed
guarantee.

The remaining v0 tests stay green.

## 11. Files

| File | Change |
|---|---|
| `renewal/providers.py` | **New.** Protocol, registry, `build_client`, both clients, IR translation. |
| `tests/test_providers.py` | **New.** Translation, transport, registry. |
| `renewal/extract/runner.py` | Emits neutral IR; `AnthropicClient` moves out; `model_id` records provider. |
| `renewal/config.py` | `provider`, `llm_base_url`, `llm_api_key`. |
| `renewal/app.py` | Constructs its client via `build_client`. |
| `renewal/extract/validate.py` | Untouched. Named here because leaving it alone is the design. |
| `renewal/web.py` | Verification rate into the review context. |
| `renewal/templates/run_review.html` | Displays it per extraction. |
| `evals/test_extraction.py` | Client via factory; per-provider-and-model baseline path. |
| `evals/accuracy.py` | Baseline path helper; verification rate in the report. |
| `evals/baseline.json` | Removed; empty. |
| `scripts/compare_versions.py` | Compares any two baseline files. |
| `.env.example` | `PROVIDER` and the per-provider key variables. |
| `pyproject.toml` | `httpx` promoted to a runtime dependency. |
| `PRIVACY.md` | Rewritten for both modes. |

## 12. Build order

Each step is shown working before the next begins.

1. `providers.py` with the protocol, both clients, and IR translation, tested without
   transport.
2. The registry, `build_client`, and configuration, including the missing-key failure.
3. `runner.py` moved onto the neutral IR; `model_id` recording provider; the scanned test
   rewritten.
4. Verification rate, computed and surfaced on the review screen.
5. Eval harness on per-provider baselines; `compare_versions.py` generalized.
6. `PRIVACY.md` and `.env.example`.
7. Prove it end to end against Ollama on a real dec page, and read the verification rate.

## 13. Deferred, with reasons

Not gaps — decisions to revisit once there is evidence.

- **Separate providers for extraction and drafting.** Two lines when a reason appears;
  today it would only create a way to leak locally-held data.
- **Per-provider structured output.** `parse_payload` already recovers JSON from fenced
  and prose-wrapped responses, so JSON mode would buy reliability that is already there.
- **Cost and token accounting.** Interesting once more than one provider is in real use;
  meaningless before.
- **A model picker in the UI.** The provider is deployment configuration, not a per-run
  choice, and making it one would let two extractions in a single comparison come from
  different models.
- **Retry and backoff.** Extraction failures already record a `failed` extraction that can
  be re-run without losing anything, which is the durable form of a retry.
