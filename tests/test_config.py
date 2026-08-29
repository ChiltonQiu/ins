"""load_settings() reads PROVIDER and the provider-keyed env vars.

No DB, no network. `load_dotenv()` inside `load_settings` will read a repo
`.env` if one exists, so every variable asserted on here is pinned with
monkeypatch rather than relying on ambient environment state.
"""

from __future__ import annotations

from renewal import config
from renewal.config import load_settings


def test_provider_grok_reads_its_key_from_xai_api_key(monkeypatch):
    monkeypatch.setenv("PROVIDER", "grok")
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    settings = load_settings()

    assert settings.provider == "grok"
    assert settings.llm_api_key == "xai-secret"


def test_provider_ollama_leaves_llm_api_key_empty_even_with_other_keys_set(
    monkeypatch,
):
    monkeypatch.setenv("PROVIDER", "ollama")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")
    monkeypatch.setenv("HF_TOKEN", "hf-secret")
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    settings = load_settings()

    assert settings.provider == "ollama"
    assert settings.llm_api_key == ""


def test_provider_defaults_to_anthropic_when_absent(monkeypatch):
    # load_settings calls load_dotenv(override=False), which backfills a deleted
    # variable from a real .env if one exists — so deleting alone would make this
    # test depend on whether the checkout happens to have one. Neutralising
    # load_dotenv is what makes "absent" actually mean absent. Pinning PROVIDER
    # to "" would not do: os.environ.get("PROVIDER", "anthropic") returns "" for
    # an empty variable rather than taking the default.
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("PROVIDER", raising=False)

    settings = load_settings()

    assert settings.provider == "anthropic"


def test_llm_base_url_is_read_when_set(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:8000/v1")

    settings = load_settings()

    assert settings.llm_base_url == "http://localhost:8000/v1"


def test_llm_base_url_is_none_when_absent(monkeypatch):
    # Neutralise load_dotenv for the reason above, so "absent" is not quietly
    # refilled from a real .env.
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)

    settings = load_settings()

    assert settings.llm_base_url is None


def test_llm_base_url_is_none_when_set_to_empty_string(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "")

    settings = load_settings()

    assert settings.llm_base_url is None
