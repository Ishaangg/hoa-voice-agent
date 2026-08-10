import asyncio

import pytest
from pipecat.frames.frames import (
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from hoa_voice_agent.voice.interim_transcript_recovery import (
    InterimTranscriptRecoveryProcessor,
)


@pytest.mark.asyncio
async def test_recovery_commits_latest_interim_when_final_transcript_never_arrives() -> None:
    processor = InterimTranscriptRecoveryProcessor(fallback_delay_secs=0.01)
    pushed = []

    async def capture(frame, _direction):
        pushed.append(frame)

    processor.push_frame = capture
    await processor.process_frame(
        InterimTranscriptionFrame("okay, ma'am. please.", "caller", "now"),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.2), FrameDirection.DOWNSTREAM
    )
    await asyncio.sleep(0.03)

    synthetic = [frame for frame in pushed if isinstance(frame, TranscriptionFrame)]
    assert len(synthetic) == 1
    assert synthetic[0].text == "okay, ma'am. please."
    assert synthetic[0].finalized is True


@pytest.mark.asyncio
async def test_recovery_does_not_duplicate_a_late_final_transcript() -> None:
    processor = InterimTranscriptRecoveryProcessor(fallback_delay_secs=0.02)
    pushed = []

    async def capture(frame, _direction):
        pushed.append(frame)

    processor.push_frame = capture
    await processor.process_frame(
        InterimTranscriptionFrame("okay, ma'am. please.", "caller", "now"),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.2), FrameDirection.DOWNSTREAM
    )
    await asyncio.sleep(0.03)
    await processor.process_frame(
        TranscriptionFrame("okay, ma'am. please.", "caller", "later", finalized=True),
        FrameDirection.DOWNSTREAM,
    )

    assert [frame.text for frame in pushed if isinstance(frame, TranscriptionFrame)] == [
        "okay, ma'am. please."
    ]


@pytest.mark.asyncio
async def test_recovery_handles_stable_transcript_without_utterance_finalization() -> None:
    processor = InterimTranscriptRecoveryProcessor(fallback_delay_secs=0.01)
    pushed = []

    async def capture(frame, _direction):
        pushed.append(frame)

    processor.push_frame = capture
    await processor.process_frame(
        TranscriptionFrame(
            "mujhe teen logon ke liye kamra chahiye",
            "caller",
            "now",
            finalized=False,
        ),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.2), FrameDirection.DOWNSTREAM
    )
    await asyncio.sleep(0.03)

    finalized = [
        frame
        for frame in pushed
        if isinstance(frame, TranscriptionFrame) and frame.finalized
    ]
    assert len(finalized) == 1
    assert finalized[0].text == "mujhe teen logon ke liye kamra chahiye"


@pytest.mark.asyncio
async def test_recovery_drops_a_longer_late_final_instead_of_creating_a_second_turn() -> None:
    processor = InterimTranscriptRecoveryProcessor(fallback_delay_secs=0.01)
    pushed = []

    async def capture(frame, _direction):
        pushed.append(frame)

    processor.push_frame = capture
    await processor.process_frame(
        InterimTranscriptionFrame("mujhe teen logon", "caller", "now"),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(
        VADUserStoppedSpeakingFrame(stop_secs=0.2), FrameDirection.DOWNSTREAM
    )
    await asyncio.sleep(0.03)
    await processor.process_frame(
        TranscriptionFrame(
            "mujhe teen logon ke liye kamra chahiye",
            "caller",
            "later",
            finalized=True,
        ),
        FrameDirection.DOWNSTREAM,
    )

    finalized = [
        frame
        for frame in pushed
        if isinstance(frame, TranscriptionFrame) and frame.finalized
    ]
    assert [frame.text for frame in finalized] == ["mujhe teen logon"]
