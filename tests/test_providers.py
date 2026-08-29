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
    assert body["max_tokens"] == 8192
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


def test_custom_with_a_base_url_and_no_key_builds_a_client():
    """custom is the documented route to vLLM, LM Studio, TGI and llama.cpp,
    none of which require auth by default; the app must not refuse to start
    against exactly the keyless local servers the privacy story depends on."""
    client = build_client(
        _settings(provider="custom", llm_base_url="http://localhost:8000/v1")
    )
    assert client.base_url == "http://localhost:8000/v1"


def test_custom_with_a_base_url_and_a_key_still_builds_a_client():
    client = build_client(
        _settings(
            provider="custom",
            llm_base_url="http://localhost:8000/v1",
            llm_api_key="k",
        )
    )
    assert client.base_url == "http://localhost:8000/v1"


def test_openai_without_a_key_still_raises_the_relaxation_does_not_leak():
    """custom no longer demands a key; that must not loosen a third-party
    preset like openai, which still needs one."""
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        build_client(_settings(provider="openai"))


from renewal.providers import PRESETS
from renewal.config import PROVIDER_KEY_ENV


def test_the_two_provider_registries_list_the_same_providers():
    assert set(PRESETS) == set(PROVIDER_KEY_ENV)


def test_anthropic_client_complete_translates_content_through_to_anthropic():
    """Nothing else proves complete() passes translated content rather than
    the raw IR blocks straight through to the SDK."""

    class _TextBlock:
        def __init__(self, text):
            self.type = "text"
            self.text = text

    class _Message:
        def __init__(self, content):
            self.content = content

    class _StubMessages:
        def __init__(self):
            self.calls = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return _Message([_TextBlock("ok")])

    class _StubAnthropic:
        def __init__(self):
            self.messages = _StubMessages()

    client = AnthropicClient(api_key="sk-x")
    stub = _StubAnthropic()
    client._client = stub

    result = client.complete(
        model="claude-opus-5",
        system="be exact",
        content=[text_block("hi"), image_block(PNG)],
    )

    assert result == "ok"
    sent = stub.messages.calls[0]["messages"][0]["content"]
    assert sent[0] == {"type": "text", "text": "hi"}
    assert sent[1]["type"] == "image"
    assert sent[1]["source"]["type"] == "base64"
    assert sent[1]["source"]["media_type"] == "image/png"
    assert base64.b64decode(sent[1]["source"]["data"]) == PNG


def test_openai_client_raises_a_diagnostic_when_choices_is_missing():
    """A 200 carrying an error body must not surface as a bare KeyError."""

    def handler(request):
        return httpx.Response(200, json={"error": "backend exploded"})

    client = OpenAICompatClient("http://x/v1", "", transport=_stub(handler))
    with pytest.raises(ValueError, match="unexpected response shape"):
        client.complete(model="m", system="s", content=[text_block("hi")])


def test_openai_client_raises_a_diagnostic_when_content_is_null():
    def handler(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": None}}]}
        )

    client = OpenAICompatClient("http://x/v1", "", transport=_stub(handler))
    with pytest.raises(ValueError, match="unexpected response shape"):
        client.complete(model="m", system="s", content=[text_block("hi")])
