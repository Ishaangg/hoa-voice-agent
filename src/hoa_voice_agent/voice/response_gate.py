"""Guards around one committed user turn producing one response.

Pipecat already cancels downstream work when it broadcasts an
``InterruptionFrame``.  The application layer still needs an idempotency guard:
the same ``LLMContextFrame`` can be observed more than once when STT emits a
final segment followed by a turn-final segment.  Without a guard, the workflow
can persist and speak the same deterministic response twice.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any

from loguru import logger
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class ResponseTurnGate:
    """Accept each context user-message object once per voice session.

    The key uses the complete message content, not only the latest transcript
    object's identity. Pipecat may rebuild equivalent message dictionaries
    when STT emits a final segment and a turn-final segment; identity-only
    de-duplication would process both. Saying the same thing twice remains
    valid when the surrounding conversation has changed. A bounded ordered set
    prevents an unusually long call from growing memory without bound.
    """

    def __init__(self, *, max_seen_turns: int = 256) -> None:
        if max_seen_turns < 1:
            raise ValueError("max_seen_turns must be at least 1")
        self._max_seen_turns = max_seen_turns
        self._seen: OrderedDict[tuple[tuple[str, str], ...], None] = OrderedDict()
        self._interruption_epoch = 0

    @property
    def interruption_epoch(self) -> int:
        return self._interruption_epoch

    def mark_interruption(self) -> int:
        """Advance the response epoch when a caller barges in."""

        self._interruption_epoch += 1
        return self._interruption_epoch

    def accept_context(self, messages: list[Any]) -> bool:
        """Return whether this context contains a new user turn."""

        latest_user_message = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, dict) and message.get("role") == "user"
            ),
            None,
        )
        if latest_user_message is None:
            # A context without a user message cannot start a response, but
            # allowing it through keeps the processor compatible with startup
            # and provider control frames.
            return True

        key = tuple(
            (
                str(message.get("role") or ""),
                str(message.get("content") or ""),
            )
            for message in messages
            if isinstance(message, dict)
        )
        if key in self._seen:
            logger.warning("Dropping duplicate voice response for context signature {}", key)
            return False

        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > self._max_seen_turns:
            self._seen.popitem(last=False)
        return True


class VoiceOutputGateProcessor(FrameProcessor):
    """Drop response frames that belong to an interrupted or duplicate turn.

    Pipecat cancels the provider tasks when an ``InterruptionFrame`` arrives.
    This extra gate protects the transport from a provider that races and emits
    one late chunk after cancellation. It also gives deterministic
    ``TTSSpeakFrame`` responses an explicit response epoch.
    """

    _EPOCH_METADATA_KEY = "voice_response_epoch"

    def __init__(self) -> None:
        super().__init__()
        self._epoch = 0
        self._active_llm_epoch: int | None = None

    @property
    def epoch(self) -> int:
        return self._epoch

    def mark_interruption(self) -> int:
        self._epoch += 1
        self._active_llm_epoch = None
        return self._epoch

    def should_forward(self, frame: Frame) -> bool:
        """Apply the gate's pure frame decision; useful for regression tests."""

        if isinstance(frame, InterruptionFrame):
            self.mark_interruption()
            return True
        if isinstance(frame, LLMFullResponseStartFrame):
            if self._active_llm_epoch is not None:
                logger.warning("Dropping duplicate LLM response start in epoch {}", self._epoch)
                return False
            self._active_llm_epoch = self._epoch
            return True
        if isinstance(frame, LLMTextFrame):
            return self._active_llm_epoch == self._epoch
        if isinstance(frame, LLMFullResponseEndFrame):
            accepted = self._active_llm_epoch == self._epoch
            self._active_llm_epoch = None
            return accepted
        if isinstance(frame, TTSSpeakFrame):
            frame_epoch = frame.metadata.get(self._EPOCH_METADATA_KEY)
            if frame_epoch is None:
                return True
            if frame_epoch != self._epoch:
                logger.warning(
                    "Dropping stale deterministic response epoch={} current_epoch={}",
                    frame_epoch,
                    self._epoch,
                )
                return False
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        # Mark before the base processor handles its urgent interruption path.
        if isinstance(frame, InterruptionFrame):
            self.mark_interruption()
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            return

        await super().process_frame(frame, direction)
        if self.should_forward(frame):
            await self.push_frame(frame, direction)
