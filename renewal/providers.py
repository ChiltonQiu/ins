"""Model providers.

The extraction pipeline speaks one neutral content format. Each client
translates that format to its own wire format, so adding a provider never
touches the pipeline and no vendor's message shape leaks into the runner.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Protocol

import httpx

from renewal.config import PROVIDER_KEY_ENV, Settings


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
            "max_tokens": 8192,
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
            body = response.json()
            try:
                content_text = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                content_text = None
            if content_text is None:
                raise ValueError(
                    f"unexpected response shape from {self.base_url}"
                )
            return content_text


@dataclass(frozen=True)
class Preset:
    base_url: str | None
    requires_key: bool = True


PRESETS: dict[str, Preset] = {
    "anthropic": Preset(base_url=None),
    "openai": Preset(base_url="https://api.openai.com/v1"),
    "grok": Preset(base_url="https://api.x.ai/v1"),
    "ollama": Preset(base_url="http://localhost:11434/v1"),
    "huggingface": Preset(base_url="https://router.huggingface.co/v1"),
    "custom": Preset(base_url=None, requires_key=False),
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
    if key_env and PRESETS[settings.provider].requires_key and not settings.llm_api_key:
        raise ValueError(f"provider {settings.provider!r} needs {key_env}")

    return OpenAICompatClient(base_url, settings.llm_api_key)
