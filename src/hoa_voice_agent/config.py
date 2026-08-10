from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "HOA Voice Agent"
    app_env: str = "development"
    database_url: str = "sqlite+aiosqlite:///./hoa_voice_agent.db"
    # Local tests and the demo may create tables automatically. Production
    # deployments should run `alembic upgrade head` and set this to false.
    database_auto_create: bool = True
    redis_url: str = "redis://localhost:6379/0"
    enable_redis: bool = False
    log_level: str = "INFO"
    # Retained for the HOA incident extractor and conversation responder.
    groq_api_key: str | None = None
    groq_llm_base_url: str = "https://api.openai.com/v1"
    groq_llm_model: str = "gpt-5.4-nano"
    # Hotel PMS tool selection is isolated from the HOA responder so it can
    # use a different provider, model, and latency profile.
    hotel_llm_api_key: str | None = None
    hotel_llm_base_url: str = "https://api.openai.com/v1"
    hotel_llm_model: str = "gpt-5.6-luna"
    hotel_llm_reasoning_effort: Literal[
        "none", "low", "medium", "high", "xhigh", "max"
    ] = "none"
    hotel_audit_log_path: str = "logs/hotel_conversation_audit.jsonl"
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
    outbox_enabled: bool = False
    # Production runs the outbox as its own worker service. Set this true only
    # for a single-process local deployment.
    outbox_worker_in_process: bool = False
    outbox_poll_interval_seconds: float = 1.0
    outbox_batch_size: int = 20
    outbox_max_attempts: int = 8
    outbox_processing_timeout_seconds: int = 300

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @model_validator(mode="after")
    def require_postgres_in_production(self) -> "Settings":
        if self.app_env == "production" and not self.database_url.startswith("postgresql"):
            raise ValueError(
                "DATABASE_URL must use PostgreSQL when APP_ENV=production; "
                "run Alembic against that database before starting the service."
            )
        if self.app_env == "production" and self.database_auto_create:
            raise ValueError(
                "DATABASE_AUTO_CREATE must be false when APP_ENV=production; "
                "run `alembic upgrade head` before starting the service."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
