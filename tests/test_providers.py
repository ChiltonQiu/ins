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
