from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class VoiceRuntimeSettings(BaseSettings):
    """Configuration for the separately-run Pipecat browser voice process."""

    database_url: str = "sqlite+aiosqlite:///./hoa_voice_agent.db"
    # Deepgram runs as a managed streaming service, so the caller experience
    # does not depend on this machine's GPU, CUDA, or local model downloads.
    voice_stt_provider: Literal["nvidia", "deepgram"] = "deepgram"
    deepgram_api_key: str | None = None
    deepgram_stt_model: str = "nova-3"
    # `multi` reliably covers a resident who moves between Hindi and English
    # in one call. Deployments serving Hindi-only callers can set this to `hi`.
    deepgram_stt_language: Literal["hi", "multi"] = "multi"
    nvidia_api_key: str | None = None
    nvidia_stt_server: str = "grpc.nvcf.nvidia.com:443"
    nvidia_tts_server: str = "grpc.nvcf.nvidia.com:443"
    nvidia_stt_function_id: str = "bb0837de-8c7b-481f-9ec8-ef5663e9c1fa"
    nvidia_stt_model: str = "nemotron-asr-streaming"
    voice_default_language: Literal["en", "hi"] = "en"
    cartesia_api_key: str | None = None
    cartesia_tts_model: str = "sonic-3.5"
    # Voice IDs are selected from Cartesia's voice library for a calm,
    # professional resident-support delivery.
    cartesia_tts_english_voice: str = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
    cartesia_tts_hindi_voice: str = "95d51f79-c397-46f9-b49a-23763d3eaa2d"
    cartesia_tts_speed: float = 1.0
    cartesia_tts_emotion: str = "calm"
    nvidia_llm_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_llm_model: str = "nvidia/nemotron-3-nano-30b-a3b"
    nvidia_use_ssl: bool | None = None
    voice_demo_caller_phone: str = "+15550101111"
    # Fast defaults for short, single-user calls. Raise turn-stop to 0.8-1.0 in noisy rooms.
    voice_vad_stop_secs: float = 0.15
    voice_turn_stop_secs: float = 0.45
    voice_llm_max_tokens: int = 32
    sms_mode: Literal["fake", "twilio"] = "fake"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_phone: str | None = None
    # The exact public HTTPS URL configured on the Twilio phone number, and the
    # matching secure WebSocket URL that receives the Media Stream.
    twilio_voice_webhook_url: str | None = None
    twilio_voice_stream_url: str | None = None
    twilio_validate_webhooks: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Optional values copied from .env.example may be deliberately blank.
        env_ignore_empty=True,
        extra="ignore",
    )

    @model_validator(mode="after")
    def require_key_for_nvidia_cloud(self) -> "VoiceRuntimeSettings":
        cloud_servers = (self.nvidia_stt_server, self.nvidia_tts_server)
        uses_nvidia_cloud = any("nvcf.nvidia.com" in server for server in cloud_servers)
        if (self.voice_stt_provider == "nvidia" or uses_nvidia_cloud) and not self.nvidia_api_key:
            raise ValueError(
                "NVIDIA_API_KEY is required when using NVIDIA's hosted Nemotron Speech endpoints. "
                "For self-hosted NIM, set NVIDIA_STT_SERVER and NVIDIA_TTS_SERVER "
                "to local endpoints."
            )
        if self.voice_stt_provider == "deepgram" and not self.deepgram_api_key:
            raise ValueError(
                "DEEPGRAM_API_KEY is required when VOICE_STT_PROVIDER=deepgram."
            )
        if not self.cartesia_api_key:
            raise ValueError("CARTESIA_API_KEY is required for Cartesia voice output.")
        if self.sms_mode == "twilio" and not all(
            (self.twilio_account_sid, self.twilio_auth_token, self.twilio_from_phone)
        ):
            raise ValueError(
                "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_FROM_PHONE are required "
                "when SMS_MODE=twilio."
            )
        voice_urls = (self.twilio_voice_webhook_url, self.twilio_voice_stream_url)
        if any(voice_urls) and not all(voice_urls):
            raise ValueError(
                "TWILIO_VOICE_WEBHOOK_URL and TWILIO_VOICE_STREAM_URL must be set together."
            )
        if all(voice_urls):
            if not all((self.twilio_account_sid, self.twilio_auth_token, self.twilio_from_phone)):
                raise ValueError(
                    "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_FROM_PHONE are required "
                    "for Twilio Voice."
                )
            if not self.twilio_voice_webhook_url.startswith("https://"):
                raise ValueError("TWILIO_VOICE_WEBHOOK_URL must use https://.")
            if not self.twilio_voice_stream_url.startswith("wss://"):
                raise ValueError("TWILIO_VOICE_STREAM_URL must use wss://.")
        return self

    def use_ssl_for(self, server: str) -> bool:
        """Use TLS for NVIDIA Cloud unless explicitly overridden for a deployment."""
        if self.nvidia_use_ssl is not None:
            return self.nvidia_use_ssl
        return "nvcf.nvidia.com" in server
