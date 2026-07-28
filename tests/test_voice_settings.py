import pytest
from pydantic import ValidationError

from hoa_voice_agent.voice.settings import VoiceRuntimeSettings


def test_hosted_nvidia_endpoints_require_an_api_key() -> None:
    with pytest.raises(ValidationError, match="NVIDIA_API_KEY"):
        VoiceRuntimeSettings(nvidia_api_key=None)


def test_local_whisper_does_not_require_an_nvidia_api_key() -> None:
    settings = VoiceRuntimeSettings(
        voice_stt_provider="local_whisper",
        nvidia_api_key=None,
        nvidia_stt_server="localhost:50051",
        nvidia_tts_server="localhost:50051",
    )

    assert settings.voice_stt_provider == "local_whisper"


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
    assert settings.voice_turn_stop_secs == 0.6
    assert settings.voice_llm_max_tokens == 48


def test_default_tts_voice_uses_a_calm_emotional_style() -> None:
    settings = VoiceRuntimeSettings(nvidia_api_key="test-key")

    assert settings.nvidia_tts_voice == "Magpie-Multilingual.EN-US.Aria.Calm"
    assert settings.nvidia_tts_greeting_voice == "Magpie-Multilingual.EN-US.Aria.Happy"


def test_twilio_mode_requires_all_twilio_credentials() -> None:
    with pytest.raises(ValidationError, match="TWILIO_ACCOUNT_SID"):
        VoiceRuntimeSettings(
            nvidia_api_key="test-key",
            sms_mode="twilio",
            twilio_account_sid=None,
            twilio_auth_token=None,
            twilio_from_phone=None,
        )
