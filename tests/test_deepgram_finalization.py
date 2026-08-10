from types import SimpleNamespace

from pipecat.frames.frames import TranscriptionFrame, VADUserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection

from hoa_voice_agent.voice.deepgram_finalization import (
    DeepgramFinalizationProcessor,
)


class _Telemetry:
    def __init__(self) -> None:
        self.events = []

    def record(self, metric_name, value_ms=None, *, metadata=None) -> None:
        self.events.append((metric_name, value_ms, metadata or {}))


async def test_speech_final_marks_only_the_completed_deepgram_turn() -> None:
    processor = DeepgramFinalizationProcessor(telemetry=_Telemetry())
    pushed = []

    async def capture(frame, _direction):
        pushed.append(frame)

    processor.push_frame = capture
    await processor.process_frame(
        TranscriptionFrame(
            "haan kar do",
            "caller",
            "now",
            result=SimpleNamespace(speech_final=True),
        ),
        FrameDirection.DOWNSTREAM,
    )

    assert len(pushed) == 1
    assert pushed[0].finalized is True


async def test_stable_segment_without_speech_final_is_not_treated_as_turn_end() -> None:
    processor = DeepgramFinalizationProcessor()
    pushed = []

    async def capture(frame, _direction):
        pushed.append(frame)

    processor.push_frame = capture
    await processor.process_frame(
        TranscriptionFrame(
            "mujhe standard",
            "caller",
            "now",
            result=SimpleNamespace(speech_final=False),
        ),
        FrameDirection.DOWNSTREAM,
    )

    assert len(pushed) == 1
    assert pushed[0].finalized is False


async def test_vad_to_finalization_latency_is_recorded_without_transcript_text() -> None:
    telemetry = _Telemetry()
    processor = DeepgramFinalizationProcessor(telemetry=telemetry)
    pushed = []

    async def capture(frame, _direction):
        pushed.append(frame)

    processor.push_frame = capture
    await processor.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.2), FrameDirection.DOWNSTREAM
    )
    await processor.process_frame(
        TranscriptionFrame(
            "do guests",
            "caller",
            "later",
            result=SimpleNamespace(speech_final=True),
        ),
        FrameDirection.DOWNSTREAM,
    )

    final_events = [event for event in telemetry.events if event[0] == "stt_final"]
    assert len(final_events) == 1
    assert final_events[0][2] == {"source": "deepgram_speech_final"}
    assert "do guests" not in str(telemetry.events)
