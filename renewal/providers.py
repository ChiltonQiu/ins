"""Model providers.

The extraction pipeline speaks one neutral content format. Each client
translates that format to its own wire format, so adding a provider never
touches the pipeline and no vendor's message shape leaks into the runner.
"""

from __future__ import annotations

import base64
from typing import Protocol

import httpx


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
