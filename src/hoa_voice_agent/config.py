from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "HOA Voice Agent"
    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./hoa_voice_agent.db"
    redis_url: str = "redis://localhost:6379/0"
    enable_redis: bool = False
    log_level: str = "INFO"
    nvidia_api_key: str | None = None
    nvidia_llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_llm_model: str = "nvidia/nemotron-3-nano-30b-a3b"
    operations_mode: Literal["fake", "webhook"] = "fake"
    operations_webhook_base_url: str | None = None
    operations_webhook_bearer_token: str | None = None
    sms_mode: Literal["fake", "twilio", "webhook"] = "fake"
    vendor_notification_channel: Literal["whatsapp", "sms"] = "whatsapp"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_phone: str | None = None
    twilio_whatsapp_from: str | None = None
    twilio_whatsapp_content_sid: str | None = None
    twilio_whatsapp_webhook_url: str | None = None
    twilio_validate_webhooks: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
