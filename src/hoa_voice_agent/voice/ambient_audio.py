"""Subtle outbound room tone for a more natural voice-session soundscape."""

from __future__ import annotations

import math
import random
from array import array
from collections.abc import Mapping

from pipecat.audio.mixers.base_audio_mixer import BaseAudioMixer
from pipecat.frames.frames import (
    MixerControlFrame,
    MixerEnableFrame,
    MixerUpdateSettingsFrame,
)


class AmbientNoiseMixer(BaseAudioMixer):
    """Mix a very quiet, continuous room-tone texture into outbound PCM.

    The texture is intentionally synthetic and restrained: a slowly varying
    noise floor, a little air noise, and barely audible low-frequency room
    tone. It is generated in memory so the voice runtime does not need an
    audio-file or decoder dependency. The mixer expects mono, signed 16-bit
    PCM, which is the format used by the current WebRTC and Twilio outputs.
    """

    def __init__(
        self,
        *,
        volume: float = 0.4,
        enabled: bool = True,
        seed: int | None = None,
    ) -> None:
        if not 0.0 <= volume <= 1.0:
            raise ValueError("Ambient mixer volume must be between 0 and 1")
        self._volume = volume
        self._enabled = enabled
        self._seed = seed
        self._sample_rate = 24000
        self._sample_index = 0
        self._filtered_noise = 0.0
        self._random = random.Random(seed)

    async def start(self, sample_rate: int) -> None:
        self._sample_rate = sample_rate
        self._sample_index = 0
        self._filtered_noise = 0.0
        self._random = random.Random(self._seed)

    async def stop(self) -> None:
        pass

    async def process_frame(self, frame: MixerControlFrame) -> None:
        if isinstance(frame, MixerEnableFrame):
            self._enabled = frame.enable
        elif isinstance(frame, MixerUpdateSettingsFrame):
            settings: Mapping[str, object] = frame.settings
            if "volume" in settings:
                volume = settings["volume"]
                if not isinstance(volume, (int, float)) or not 0.0 <= volume <= 1.0:
                    raise ValueError("Ambient mixer volume must be between 0 and 1")
                self._volume = float(volume)
            if "enabled" in settings:
                enabled = settings["enabled"]
                if not isinstance(enabled, bool):
                    raise ValueError("Ambient mixer enabled must be a boolean")
                self._enabled = enabled

    async def mix(self, audio: bytes) -> bytes:
        if not self._enabled or self._volume <= 0.0 or not audio or len(audio) % 2:
            return audio

        samples = array("h", audio)
        for index, original in enumerate(samples):
            white_noise = self._random.uniform(-1.0, 1.0)
            self._filtered_noise = 0.95 * self._filtered_noise + 0.05 * white_noise
            elapsed = (self._sample_index + index) / self._sample_rate
            room_tone = (
                0.004 * math.sin(2.0 * math.pi * 58.0 * elapsed)
                + 0.002 * math.sin(2.0 * math.pi * 116.0 * elapsed)
            )
            ambient = (
                0.04 * self._filtered_noise
                + 0.012 * white_noise
                + room_tone
            ) * 32767.0 * self._volume
            samples[index] = max(-32768, min(32767, round(original + ambient)))

        self._sample_index += len(samples)
        return samples.tobytes()


__all__ = ["AmbientNoiseMixer"]
