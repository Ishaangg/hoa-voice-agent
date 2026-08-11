from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from hoa_voice_agent.voice.language import LanguageMode


class VoiceRuntimeSettings(BaseSettings):
    """Configuration for the separately-run Pipecat browser voice process."""

    database_url: str = "sqlite+aiosqlite:///./hoa_voice_agent.db"
    database_auto_create: bool = True
    # Keep the voice transport shared while selecting a domain-specific
    # conversation service. HOA remains the default for backwards compatibility.
    voice_domain: Literal["hoa", "hotel"] = "hoa"
    hotel_demo_hotel_id: str = "40000000-0000-0000-0000-000000000001"
    # Deprecated compatibility override. New deployments should select one
    # session language with VOICE_DEFAULT_LANGUAGE.
    hotel_response_language: LanguageMode | None = None
    # Deepgram runs as a managed streaming service, so the caller experience
    # does not depend on this machine's GPU, CUDA, or local model downloads.
    voice_stt_provider: Literal["nvidia", "deepgram", "deepgram_flux"] = "deepgram_flux"
    deepgram_api_key: str | None = None
    deepgram_stt_model: str = "nova-3"
    # The language adapter owns STT selection for en/hi/hinglish. This optional
    # field remains available for a future provider-specific language code.
    deepgram_stt_language: str = "multi"
    # Deepgram endpointing gives stable transcript segments enough silence to
    # settle while the VAD-triggered Finalize remains the primary turn flush.
    deepgram_stt_endpointing_ms: int = Field(default=200, ge=10, le=2000)
    # Pipecat uses this as the STT safety timer. Keep it configurable so it can
    # be replaced with the measured deployment p99 without changing code.
    deepgram_stt_ttfs_p99_latency: float = Field(default=0.35, gt=0.0, le=10.0)
    # Provider-side vocabulary hints are optional and deployment-configured.
    # Leaving this empty keeps Nova/Flux comparisons unbiased.
    deepgram_stt_keyterms: list[str] = Field(default_factory=list)
    # Flux uses the v2 endpoint and owns turn detection, so its controls stay
    # separate from Nova's endpointing and local VAD settings.
    deepgram_flux_stt_model: str = "flux-general-multi"
    deepgram_flux_language_hints: list[str] = Field(default_factory=lambda: ["en", "hi"])
    deepgram_flux_eot_threshold: float | None = Field(default=None, ge=0.5, le=0.9)
    deepgram_flux_eager_eot_threshold: float | None = Field(default=None, ge=0.3, le=0.9)
    deepgram_flux_eot_timeout_ms: int | None = Field(default=None, ge=500, le=60000)
    # NVIDIA is retained only for the explicitly selected legacy STT fallback.
    nvidia_api_key: str | None = None
    nvidia_stt_server: str = "grpc.nvcf.nvidia.com:443"
    nvidia_tts_server: str = "grpc.nvcf.nvidia.com:443"
    nvidia_stt_function_id: str = "bb0837de-8c7b-481f-9ec8-ef5663e9c1fa"
    nvidia_stt_model: str = "nemotron-asr-streaming"
    # The first hotel demo profile is Hinglish. The same setting is used for
    # the LLM response style and TTS profile, so they cannot drift apart.
    voice_default_language: LanguageMode = "hinglish"
    # A quiet outbound room tone makes short pauses feel less like a silent
    # digital connection. The mixer never touches caller audio or STT input.
    voice_ambient_enabled: bool = True
    voice_ambient_volume: float = Field(default=0.4, ge=0.0, le=1.0)
    cartesia_api_key: str | None = None
    cartesia_tts_model: str = "sonic-3.5"
    # Voice IDs are selected from Cartesia's voice library for a calm,
    # professional resident-support delivery.
    cartesia_tts_english_voice: str = "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"
    cartesia_tts_hindi_voice: str = "95d51f79-c397-46f9-b49a-23763d3eaa2d"
    # Optional session-wide override selected from Cartesia's voice library.
    # The voice ID controls accent/style; the locale tells Cartesia how to
    # pronounce the text. When left blank, the English/Hindi defaults below
    # continue to follow VOICE_DEFAULT_LANGUAGE.
    cartesia_tts_voice: str | None = None
    cartesia_tts_language: str | None = None
    cartesia_tts_speed: float = 1.05
    cartesia_tts_emotion: str = "content"
    # Stream LLM text into Cartesia as it arrives. A small server-side buffer
    # preserves prosody without waiting for a complete sentence.
    cartesia_tts_text_mode: Literal["sentence", "token"] = "token"
    # Zero disables Cartesia's extra server-side token buffer. The pipeline
    # already controls aggregation, so another queue only delays first audio.
    cartesia_tts_max_buffer_delay_ms: int = 120
    # Fish Audio can stream raw PCM over its WebSocket API. Keep this provider
    # opt-in so an existing Cartesia deployment is not changed accidentally.
    voice_tts_provider: Literal["cartesia", "fish"] = "cartesia"
    fish_api_key: str | None = None
    fish_tts_model: str = "s2.1-pro-free"
    # Fish requires a voice/model reference ID for single-speaker synthesis.
    fish_tts_reference_id: str | None = None
    # Fish's HTTP/WebSocket API also supports ``low``. The installed SDK's
    # type annotation still only lists normal/balanced, so the adapter sends
    # low at the request boundary without relying on the stale annotation.
    fish_tts_latency: Literal["normal", "balanced", "low"] = "low"
    fish_tts_chunk_length: int = 100
    # Lower values let Fish start an utterance before its text buffer fills.
    fish_tts_min_chunk_length: int = 0
    # This saves a little context work between streamed chunks. A single
    # sentence is still synthesized as one coherent turn in our adapter.
    fish_tts_condition_on_previous_chunks: bool = False
    # Open Fish's WebSocket when the LLM turn starts so the handshake overlaps
    # LLM generation instead of delaying the first audio chunk.
    fish_tts_preconnect: bool = True
    fish_tts_sample_rate: int = 24000
    # Slightly quicker than default speech while remaining conversational.
    fish_tts_speed: float = 1.08
    fish_tts_temperature: float = 0.8
    fish_tts_top_p: float = 0.8
    fish_tts_style: str = "warm, conversational"
    fish_tts_text_mode: Literal["sentence", "token"] = "sentence"
    groq_api_key: str | None = None
    # These legacy names configure the existing HOA LLM only.
    groq_llm_base_url: str = "https://api.openai.com/v1"
    groq_llm_model: str = "gpt-5.4-nano"
    # The hotel interpreter has its own provider configuration. The demo uses
    # Groq's OpenAI-compatible endpoint and reuses GROQ_API_KEY; a different
    # endpoint can still use HOTEL_LLM_API_KEY.
    hotel_llm_api_key: str | None = None
    hotel_llm_base_url: str = "https://api.groq.com/openai/v1"
    hotel_llm_model: str = "qwen/qwen3.6-27b"
    hotel_llm_reasoning_effort: Literal[
        "none", "low", "medium", "high", "xhigh", "max"
    ] = "none"
    # Keep the voice call's LLM and PMS evidence local and outside the database.
    hotel_audit_log_path: str = "logs/hotel_conversation_audit.jsonl"
    nvidia_use_ssl: bool | None = None
    voice_demo_caller_phone: str = "+15550101111"
    # VAD is language-agnostic. These tolerant defaults preserve softer Hindi
    # syllables and code-switched phrases without waiting for a long silence.
    voice_vad_confidence: float = 0.55
    voice_vad_start_secs: float = 0.15
    # Let Deepgram finalize promptly. The separate turn strategy still applies
    # the 650 ms conversational endpoint after this silence signal is observed.
    voice_vad_stop_secs: float = 0.2
    voice_vad_min_volume: float = 0.4
    # A 650 ms conversational pause window leaves room for ordinary
    # mid-sentence pauses after the shorter VAD endpoint fires.
    voice_turn_stop_secs: float = 0.65
    # If a provider leaves only an interim transcript, promote it after this
    # grace period instead of waiting for the aggregator watchdog.
    voice_interim_transcript_fallback_secs: float = Field(default=0.55, gt=0.0, le=2.0)
    voice_use_smart_turn: bool = False
    voice_smart_turn_stop_secs: float = 0.75
    voice_smart_turn_cpu_count: int = 1
    voice_use_interruption_gate: bool = True
    voice_interrupt_min_words: int = 2
    voice_llm_max_tokens: int = 24
    voice_enable_tracing: bool = False
    sms_mode: Literal["fake", "twilio"] = "fake"
    vendor_notification_channel: Literal["whatsapp", "sms"] = "whatsapp"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_phone: str | None = None
    twilio_whatsapp_from: str | None = None
    twilio_whatsapp_content_sid: str | None = None
    twilio_whatsapp_webhook_url: str | None = None
    # The exact public HTTPS URL configured on the Twilio phone number, and the
    # matching secure WebSocket URL that receives the Media Stream.
    twilio_voice_webhook_url: str | None = None
    twilio_voice_stream_url: str | None = None
    twilio_validate_webhooks: bool = True
    outbox_enabled: bool = False
    outbox_worker_in_process: bool = False
    outbox_poll_interval_seconds: float = 1.0
    outbox_batch_size: int = 20
    outbox_max_attempts: int = 8
    outbox_processing_timeout_seconds: int = 300

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Optional values copied from .env.example may be deliberately blank.
        env_ignore_empty=True,
        extra="ignore",
    )

    @property
    def resolved_hotel_llm_api_key(self) -> str | None:
        """Return the credential that matches the configured hotel endpoint."""

        if self.hotel_llm_base_url.rstrip("/").casefold() == "https://api.groq.com/openai/v1":
            return self.groq_api_key or self.hotel_llm_api_key
        return self.hotel_llm_api_key

    @model_validator(mode="after")
    def require_provider_keys(self) -> "VoiceRuntimeSettings":
        if self.voice_domain == "hoa" and not self.groq_api_key:
            raise ValueError("GROQ_API_KEY is required for the HOA voice LLM.")
        if self.voice_domain == "hotel" and not self.resolved_hotel_llm_api_key:
            if self.hotel_llm_base_url.rstrip("/").casefold() == "https://api.groq.com/openai/v1":
                raise ValueError(
                    "GROQ_API_KEY or HOTEL_LLM_API_KEY is required for the hotel PMS "
                    "interpreter when using Groq."
                )
            raise ValueError("HOTEL_LLM_API_KEY is required for the hotel PMS interpreter.")
        if self.voice_stt_provider == "nvidia" and not self.nvidia_api_key:
            raise ValueError(
                "NVIDIA_API_KEY is required when using NVIDIA's hosted Nemotron Speech endpoints. "
                "For self-hosted NIM, set NVIDIA_STT_SERVER and NVIDIA_TTS_SERVER "
                "to local endpoints."
            )
        if self.voice_stt_provider in {"deepgram", "deepgram_flux"} and not self.deepgram_api_key:
            raise ValueError(
                "DEEPGRAM_API_KEY is required when VOICE_STT_PROVIDER uses Deepgram."
            )
        if self.voice_tts_provider == "cartesia" and not self.cartesia_api_key:
            raise ValueError("CARTESIA_API_KEY is required when VOICE_TTS_PROVIDER=cartesia.")
        if (
            self.voice_tts_provider == "cartesia"
            and self.cartesia_tts_language not in {None, "en", "hi"}
            and not self.cartesia_tts_voice
        ):
            raise ValueError(
                "CARTESIA_TTS_VOICE is required when CARTESIA_TTS_LANGUAGE "
                "is not en or hi."
            )
        if self.voice_tts_provider == "fish":
            if not self.fish_api_key:
                raise ValueError("FISH_API_KEY is required when VOICE_TTS_PROVIDER=fish.")
            if not self.fish_tts_reference_id:
                raise ValueError(
                    "FISH_TTS_REFERENCE_ID is required when VOICE_TTS_PROVIDER=fish."
                )
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
        """Use TLS for the optional NVIDIA STT cloud endpoint."""
        if self.nvidia_use_ssl is not None:
            return self.nvidia_use_ssl
        return "nvcf.nvidia.com" in server
