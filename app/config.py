"""Settings loaded from environment variables (and `.env` when present).

The variable names match the `.env` section of README.md exactly.
pydantic-settings matches them case-insensitively, so `WA_ACCESS_TOKEN`
becomes `settings.wa_access_token`.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_LANGUAGES = ("gu-IN", "hi-IN", "en-IN")

# "value   # comment" -> "value", and "   # comment" -> "" (an empty value followed by a
# comment, e.g. `DATABASE_URL=   # optional`). Some hosting dashboards (e.g. a raw-editor
# paste) also keep inline comments as part of the value, so we strip them defensively.
_INLINE_COMMENT = re.compile(r"(^|\s+)#.*$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    admin_api_key: str = ""

    # WhatsApp (Meta)
    wa_access_token: str = ""
    wa_phone_number_id: str = ""
    wa_business_account_id: str = ""
    wa_app_secret: str = ""
    wa_verify_token: str = ""
    graph_api_version: str = "v23.0"

    # Google Gemini (answers + voice, one API key)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_thinking_level: str = "low"
    gemini_stt_model: str = "gemini-3.8-flash"  # voice note -> text
    gemini_tts_model: str = "gemini-3.1-flash-tts-preview"  # text -> voice
    gemini_tts_voice: str = "Sulafat"  # a warm female prebuilt voice

    # Storage
    redis_url: str = "redis://localhost:6379/0"
    database_url: str = ""  # reserved for the later Postgres move

    # Bot behaviour
    default_language: str = "gu-IN"
    daily_message_limit: int = 50
    max_voice_seconds: int = 120
    voice_reply_also_text: bool = False

    # Monitoring
    sentry_dsn: str = ""

    @field_validator("*", mode="before")
    @classmethod
    def _strip_inline_comments(cls, value: Any) -> Any:
        if isinstance(value, str):
            return _INLINE_COMMENT.sub("", value).strip()
        return value

    @field_validator("default_language")
    @classmethod
    def _check_language(cls, value: str) -> str:
        if value not in SUPPORTED_LANGUAGES:
            raise ValueError(f"DEFAULT_LANGUAGE must be one of {SUPPORTED_LANGUAGES}")
        return value

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
