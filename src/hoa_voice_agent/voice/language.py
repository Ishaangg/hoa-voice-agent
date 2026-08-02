"""Language configuration for one live voice session.

The language is chosen before a call starts and remains fixed throughout that
session. A transcript must never change the TTS voice mid-response.
"""

from typing import Literal

VoiceLanguage = Literal["en", "hi"]


def tts_settings_for_language(
    language: VoiceLanguage,
    *,
    english_voice: str,
    hindi_voice: str,
) -> dict[str, str]:
    """Return fixed Cartesia voice settings for the full session."""

    if language == "hi":
        return {"voice": hindi_voice, "language": "hi"}
    return {"voice": english_voice, "language": "en"}
