from pipecat.turns.user_turn_strategies import ExternalUserTurnStrategies

from hoa_voice_agent.voice.bot import create_stt, create_turn_strategies
from hoa_voice_agent.voice.settings import VoiceRuntimeSettings


def _flux_settings(**overrides) -> VoiceRuntimeSettings:
    values = {
        "voice_domain": "hotel",
        "voice_stt_provider": "deepgram_flux",
        "deepgram_api_key": "test-deepgram-key",
        "hotel_llm_api_key": "test-hotel-key",
        "cartesia_api_key": "test-cartesia-key",
        "_env_file": None,
    }
    values.update(overrides)
    return VoiceRuntimeSettings(**values)


def test_flux_provider_constructs_the_installed_pipecat_service() -> None:
    stt = create_stt(_flux_settings())

    assert type(stt).__name__ == "DeepgramFluxSTTService"
    assert stt._settings.model == "flux-general-multi"
    assert [str(language) for language in stt._settings.language_hints] == ["en", "hi"]


def test_flux_is_the_runtime_default_profile() -> None:
    settings = VoiceRuntimeSettings(
        voice_domain="hotel",
        deepgram_api_key="test-deepgram-key",
        hotel_llm_api_key="test-hotel-key",
        cartesia_api_key="test-cartesia-key",
        _env_file=None,
    )

    assert settings.voice_stt_provider == "deepgram_flux"


def test_flux_uses_external_turn_events_instead_of_local_vad() -> None:
    strategies = create_turn_strategies(_flux_settings())

    assert isinstance(strategies, ExternalUserTurnStrategies)


def test_flux_keyterms_and_turn_controls_are_configurable() -> None:
    settings = _flux_settings(
        deepgram_stt_keyterms=["kamra", "logon", "hotel booking"],
        deepgram_flux_eot_threshold=0.8,
        deepgram_flux_eager_eot_threshold=0.5,
        deepgram_flux_eot_timeout_ms=3000,
    )
    stt = create_stt(settings)

    assert stt._settings.keyterm == ["kamra", "logon", "hotel booking"]
    assert stt._settings.eot_threshold == 0.8
    assert stt._settings.eager_eot_threshold == 0.5
    assert stt._settings.eot_timeout_ms == 3000
