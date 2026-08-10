"""Translate Deepgram's utterance boundary into Pipecat's finalization signal."""

from __future__ import annotations

import time
from typing import Any

from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from hoa_voice_agent.services.telemetry import CallTelemetry


def _speech_final(result: Any) -> bool:
    if isinstance(result, dict):
        return bool(result.get("speech_final"))
    return bool(getattr(result, "speech_final", False))


class DeepgramFinalizationProcessor(FrameProcessor):
    """Mark only Deepgram's completed utterance as finalized.

    Deepgram's ``is_final`` means that one transcript segment is stable. Its
    ``speech_final`` signal means that the utterance has ended. Pipecat's turn
    strategies need the latter represented as ``TranscriptionFrame.finalized``;
    treating every stable segment as a completed turn would cut long utterances
    into fragments.
    """

    def __init__(self, *, telemetry: CallTelemetry | None = None) -> None:
        super().__init__()
        self._telemetry = telemetry
        self._vad_stopped_at: float | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_stopped_at = None
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_stopped_at = time.monotonic()
            if self._telemetry:
                self._telemetry.record(
                    "vad_stop",
                    metadata={"stop_secs": frame.stop_secs},
                )
        elif isinstance(frame, TranscriptionFrame) and frame.text.strip():
            from_deepgram_speech_final = _speech_final(frame.result)
            if from_deepgram_speech_final and not frame.finalized:
                frame = TranscriptionFrame(
                    frame.text,
                    frame.user_id,
                    frame.timestamp,
                    frame.language,
                    result=frame.result,
                    finalized=True,
                )
            if frame.finalized and self._telemetry:
                if from_deepgram_speech_final:
                    source = "deepgram_speech_final"
                else:
                    source = "provider_finalize"
                elapsed_ms = (
                    (time.monotonic() - self._vad_stopped_at) * 1000
                    if self._vad_stopped_at is not None
                    else None
                )
                self._telemetry.record(
                    "stt_final",
                    elapsed_ms,
                    metadata={"source": source},
                )

        await self.push_frame(frame, direction)


__all__ = ["DeepgramFinalizationProcessor"]
