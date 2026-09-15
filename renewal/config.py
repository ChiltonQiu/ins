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
    extras_config: Path = Path("config/extras.yaml")
    provider: str = "anthropic"
    llm_base_url: str | None = None
    llm_api_key: str = ""
    blob_encryption_key: str = ""
    date_model: str = "claude-sonnet-5"
    date_pages: int = 3
    classification_model: str = "claude-haiku-4-5-20251001"
    inbound_provider: str = "filedrop"
    inbound_drop_dir: str = "mail"
    session_cookie_secure: bool = True
    session_ttl_hours: int = 12
    attention_premium_pct: float = 10.0
    # Operator notifications. An empty smtp_host turns them off, which is the
    # default: a misconfigured mail server must never be able to cost a
    # document.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    notify_from: str = ""
    notify_to: str = ""
    notify_min_interval_minutes: int = 60
    base_url: str = "http://127.0.0.1:8000"


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
        extras_config=Path(os.environ.get("EXTRAS_CONFIG", "config/extras.yaml")),
        attention_premium_pct=float(
            os.environ.get("ATTENTION_PREMIUM_PCT", "10")
        ),
        provider=provider,
        llm_base_url=os.environ.get("LLM_BASE_URL") or None,
        llm_api_key=os.environ.get(key_env, "") if key_env else "",
        blob_encryption_key=os.environ.get("BLOB_ENCRYPTION_KEY", ""),
        date_model=os.environ.get("DATE_MODEL", "claude-sonnet-5"),
        date_pages=int(os.environ.get("DATE_PAGES", "3")),
        classification_model=os.environ.get(
            "CLASSIFICATION_MODEL", "claude-haiku-4-5-20251001"
        ),
        inbound_provider=os.environ.get("INBOUND_PROVIDER", "filedrop"),
        inbound_drop_dir=os.environ.get("INBOUND_DROP_DIR", "mail"),
        # Defaults to true so that forgetting to configure it fails toward
        # security. Local development over plain HTTP sets it false.
        session_cookie_secure=os.environ.get(
            "SESSION_COOKIE_SECURE", "true"
        ).lower() not in ("0", "false", "no"),
        session_ttl_hours=int(os.environ.get("SESSION_TTL_HOURS", "12")),
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_username=os.environ.get("SMTP_USERNAME", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        notify_from=os.environ.get("NOTIFY_FROM", ""),
        notify_to=os.environ.get("NOTIFY_TO", ""),
        notify_min_interval_minutes=int(
            os.environ.get("NOTIFY_MIN_INTERVAL_MINUTES", "60")
        ),
        base_url=os.environ.get("BASE_URL", "http://127.0.0.1:8000"),
    )
