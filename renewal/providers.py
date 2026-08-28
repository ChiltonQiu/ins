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
