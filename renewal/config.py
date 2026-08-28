from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROVIDER_KEY_ENV: dict[str, str | None] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "grok": "XAI_API_KEY",
    "ollama": None,
    "huggingface": "HF_TOKEN",
    "custom": "LLM_API_KEY",
}


@dataclass(frozen=True)
class Settings:
    database_url: str
    blob_root: Path
    anthropic_api_key: str
    extraction_model: str
    draft_model: str
    confidence_threshold: float
    materiality_config: Path
    provider: str = "anthropic"
    llm_base_url: str | None = None
    llm_api_key: str = ""


def load_settings() -> Settings:
    load_dotenv()
    provider = os.environ.get("PROVIDER", "anthropic")
    key_env = PROVIDER_KEY_ENV.get(provider)
    return Settings(
        database_url=os.environ.get(
            "DATABASE_URL", "postgresql+psycopg:///renewal"
        ),
        blob_root=Path(os.environ.get("BLOB_ROOT", "blobs")),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
        extraction_model=os.environ.get("EXTRACTION_MODEL", "claude-opus-5"),
        draft_model=os.environ.get("DRAFT_MODEL", "claude-sonnet-5"),
        confidence_threshold=float(os.environ.get("CONFIDENCE_THRESHOLD", "0.80")),
        materiality_config=Path(
            os.environ.get("MATERIALITY_CONFIG", "config/materiality.yaml")
        ),
        provider=provider,
        llm_base_url=os.environ.get("LLM_BASE_URL") or None,
        llm_api_key=os.environ.get(key_env, "") if key_env else "",
    )
