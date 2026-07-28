import pytest
from pydantic import ValidationError

from hoa_voice_agent.voice.settings import VoiceRuntimeSettings


def test_hosted_nvidia_endpoints_require_an_api_key() -> None:
    with pytest.raises(ValidationError, match="NVIDIA_API_KEY"):
        VoiceRuntimeSettings(nvidia_api_key=None)


def test_deepgram_requires_its_own_api_key() -> None:
    with pytest.raises(ValidationError, match="DEEPGRAM_API_KEY"):
        VoiceRuntimeSettings(
            voice_stt_provider="deepgram",
            deepgram_api_key=None,
            nvidia_api_key="test-key",
        )


def test_deepgram_does_not_require_an_nvidia_stt_key() -> None:
    settings = VoiceRuntimeSettings(
        voice_stt_provider="deepgram",
        deepgram_api_key="deepgram-test-key",
        nvidia_api_key="test-key",
    )

    assert settings.voice_stt_provider == "deepgram"


def test_deepgram_defaults_to_multilingual_nova_three() -> None:
    settings = VoiceRuntimeSettings(
        deepgram_api_key="deepgram-test-key",
        nvidia_api_key="test-key",
    )

    assert settings.deepgram_stt_model == "nova-3"
    assert settings.deepgram_stt_language == "multi"


def test_self_hosted_nvidia_endpoints_do_not_require_an_api_key() -> None:
    settings = VoiceRuntimeSettings(
        nvidia_api_key=None,
        nvidia_stt_server="asr-nim:50051",
        nvidia_tts_server="tts-nim:50051",
    )

    assert settings.nvidia_api_key is None
    assert settings.use_ssl_for(settings.nvidia_stt_server) is False


def test_hosted_nvidia_endpoints_default_to_tls() -> None:
    settings = VoiceRuntimeSettings(nvidia_api_key="test-key")

    assert settings.use_ssl_for(settings.nvidia_tts_server) is True


def test_fast_voice_defaults_limit_turn_and_response_delay() -> None:
    settings = VoiceRuntimeSettings(nvidia_api_key="test-key")

    assert settings.voice_vad_stop_secs == 0.15
    assert settings.voice_turn_stop_secs == 0.45
    assert settings.voice_llm_max_tokens == 32


def test_default_cartesia_voice_uses_a_calm_professional_style() -> None:
    settings = VoiceRuntimeSettings(
        nvidia_api_key="test-key",
        voice_default_language="en",
    )

    assert settings.cartesia_tts_model == "sonic-3.5"
    assert settings.cartesia_tts_emotion == "calm"
    assert settings.cartesia_tts_english_voice
    assert settings.cartesia_tts_hindi_voice
    assert settings.voice_default_language == "en"


def test_twilio_mode_requires_all_twilio_credentials() -> None:
    with pytest.raises(ValidationError, match="TWILIO_ACCOUNT_SID"):
        VoiceRuntimeSettings(
            nvidia_api_key="test-key",
            sms_mode="twilio",
            twilio_account_sid=None,
            twilio_auth_token=None,
            twilio_from_phone=None,
        )


def test_twilio_voice_urls_must_be_configured_together() -> None:
    with pytest.raises(ValidationError, match="TWILIO_VOICE_WEBHOOK_URL"):
        VoiceRuntimeSettings(
            nvidia_api_key="test-key",
            twilio_voice_webhook_url="https://example.ngrok.app/twilio/voice",
            twilio_voice_stream_url=None,
        )


def test_twilio_voice_requires_secure_urls() -> None:
    with pytest.raises(ValidationError, match="TWILIO_VOICE_STREAM_URL must use wss"):
        VoiceRuntimeSettings(
            nvidia_api_key="test-key",
            twilio_account_sid="AC123",
            twilio_auth_token="secret",
            twilio_from_phone="+15550101000",
            twilio_voice_webhook_url="https://example.ngrok.app/twilio/voice",
            twilio_voice_stream_url="ws://example.ngrok.app/ws",
        )
