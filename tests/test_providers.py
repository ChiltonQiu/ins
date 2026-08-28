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
