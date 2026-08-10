"""Guarded user-turn starts for natural barge-in behavior.

VAD is intentionally responsive, but a one-word acknowledgement such as
"yeah" or "okay" should not normally cancel the assistant's sentence. This
strategy keeps normal user turns responsive while requiring a little more
evidence when the bot is speaking. Strong control/correction words still
interrupt immediately.

This is a low-latency guard, not a replacement for a trained semantic
interruption model. It is the safe local baseline until we have enough call
audio to evaluate a dedicated interruption classifier.
"""

from __future__ import annotations

import re

from loguru import logger
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.turns.types import ProcessFrameResult
from pipecat.turns.user_start.base_user_turn_start_strategy import (
    BaseUserTurnStartStrategy,
)


class GuardedInterruptionUserTurnStartStrategy(BaseUserTurnStartStrategy):
    """Allow natural barge-in without reacting to every backchannel."""

    _strong_single_word_interruptions = frozenset(
        {"stop", "wait", "cancel", "no", "wrong", "correction", "sorry", "actually"}
    )
    _strong_prefixes = (
        "actually ",
        "no,",
        "no ",
        "wait,",
        "wait ",
        "stop,",
        "stop ",
        "that's wrong",
        "that is wrong",
        "not ",
    )

    def __init__(self, *, min_words_while_bot_speaking: int = 2, **kwargs):
        super().__init__(**kwargs)
        if min_words_while_bot_speaking < 1:
            raise ValueError("min_words_while_bot_speaking must be at least 1")
        self._min_words_while_bot_speaking = min_words_while_bot_speaking
        self._bot_speaking = False

    async def handle_user_turn_started(self):
        self._bot_speaking = False

    async def process_frame(self, frame: Frame) -> ProcessFrameResult:
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            return ProcessFrameResult.CONTINUE
        if isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            return ProcessFrameResult.CONTINUE
        if isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            text = " ".join(frame.text.lower().split())
            if not text:
                return ProcessFrameResult.CONTINUE

            word_count = len(text.split())
            should_interrupt = (
                not self._bot_speaking
                or word_count >= self._min_words_while_bot_speaking
                or self.is_strong_interruption(text)
            )
            logger.debug(
                "Interruption gate bot_speaking={} words={} should_interrupt={} text={!r}",
                self._bot_speaking,
                word_count,
                should_interrupt,
                text,
            )
            if should_interrupt:
                await self.trigger_user_turn_started()
                return ProcessFrameResult.STOP
            await self.trigger_reset_aggregation()

        return ProcessFrameResult.CONTINUE

    @classmethod
    def is_strong_interruption(cls, text: str) -> bool:
        """Return true for short control/correction utterances."""

        normalized = " ".join(text.lower().split())
        if normalized in cls._strong_single_word_interruptions:
            return True
        if normalized.startswith(cls._strong_prefixes):
            return True
        # Catch short spoken corrections such as "not room 414" while keeping
        # the matcher bounded to the beginning of the utterance.
        return bool(re.match(r"^(?:not|wrong)\s+(?:my\s+)?(?:name|room)\b", normalized))
