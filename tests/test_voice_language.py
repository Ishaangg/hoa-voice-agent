from hoa_voice_agent.voice.language import tts_settings_for_language


def test_hindi_tts_uses_a_hindi_voice_and_locale_together() -> None:
    assert tts_settings_for_language(
        "hi",
        english_voice="english-voice-id",
        hindi_voice="hindi-voice-id",
    ) == {"voice": "hindi-voice-id", "language": "hi"}
