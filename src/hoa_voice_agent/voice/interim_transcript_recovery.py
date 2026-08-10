"""Recover a short user turn when streaming STT never emits its final frame."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class InterimTranscriptRecoveryProcessor(FrameProcessor):
    """Commit the latest interim transcript only when STT stays unfinished.

    Streaming STT providers normally emit an ``InterimTranscriptionFrame``
    followed by a final ``TranscriptionFrame``.  A telephony finalize race can
    leave only the interim frame, however.  The normal Pipecat aggregator does
    not put interim text into the LLM context, so the caller's turn can then
    end with no text at all.  This processor gives the provider a short grace
    period, then promotes the latest interim text to one synthetic final frame.

    A late final frame that repeats the recovered text is suppressed to avoid
    submitting the same caller turn twice.  If it contains a longer prefix, we
    forward only the unseen suffix; this keeps the context coherent without
    replaying the whole utterance.
    """

    def __init__(self, *, fallback_delay_secs: float = 0.55) -> None:
        super().__init__()
        if fallback_delay_secs <= 0:
            raise ValueError("fallback_delay_secs must be positive")
        self._fallback_delay_secs = fallback_delay_secs
        self._latest_candidate: InterimTranscriptionFrame | TranscriptionFrame | None = None
        self._final_received = False
        self._turn_stopped = False
        self._fallback_text: str | None = None
        self._recovery_task: asyncio.Task | None = None
        self._generation = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._generation += 1
            await self._cancel_recovery_task()
            self._latest_candidate = None
            self._final_received = False
            self._turn_stopped = False
            self._fallback_text = None
        elif isinstance(frame, InterimTranscriptionFrame):
            if self._fallback_text is None and frame.text.strip():
                self._latest_candidate = frame
                if self._turn_stopped and not self._final_received:
                    self._schedule_recovery(direction)
        elif isinstance(frame, TranscriptionFrame) and frame.text.strip():
            if self._fallback_text is not None:
                # A synthetic final has already committed this turn. A late
                # provider final cannot retract it; forwarding its suffix
                # would create a second user turn and can trigger duplicate
                # booking work.
                logger.debug("Dropping late transcript after interim recovery")
                return
            if frame.finalized:
                self._final_received = True
                await self._cancel_recovery_task()
            else:
                # ``TranscriptionFrame`` can be a stable provider segment,
                # not the end of the caller's utterance. Keep it as a recovery
                # candidate until Deepgram speech_final or the VAD grace
                # window establishes the turn boundary.
                self._latest_candidate = frame
                if self._turn_stopped and not self._final_received:
                    self._schedule_recovery(direction)
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self._turn_stopped = True
            if self._latest_candidate and not self._final_received:
                self._schedule_recovery(direction)

        await self.push_frame(frame, direction)

    def _schedule_recovery(self, direction: FrameDirection) -> None:
        generation = self._generation
        candidate = self._latest_candidate
        if candidate is None:
            return
        if self._recovery_task and not self._recovery_task.done():
            self._recovery_task.cancel()
        self._recovery_task = asyncio.create_task(
            self._recover(generation, candidate, direction),
            name=f"{self}::recover_interim_transcript",
        )

    async def _recover(
        self,
        generation: int,
        candidate: InterimTranscriptionFrame | TranscriptionFrame,
        direction: FrameDirection,
    ) -> None:
        try:
            await asyncio.sleep(self._fallback_delay_secs)
        except asyncio.CancelledError:
            return
        if (
            generation != self._generation
            or self._final_received
            or self._latest_candidate is not candidate
            or not candidate.text.strip()
        ):
            return
        self._fallback_text = candidate.text
        logger.warning(
            "STT final transcript did not arrive; recovering interim text as final transcript"
        )
        await self.push_frame(
            TranscriptionFrame(
                candidate.text,
                candidate.user_id,
                candidate.timestamp,
                candidate.language,
                result=candidate.result,
                finalized=True,
            ),
            direction,
        )

    async def _cancel_recovery_task(self) -> None:
        if self._recovery_task is None:
            return
        task = self._recovery_task
        self._recovery_task = None
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def cleanup(self) -> None:
        await self._cancel_recovery_task()
        await super().cleanup()
