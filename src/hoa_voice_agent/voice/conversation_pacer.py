"""Truthful speech bridges for voice turns that are taking noticeable time."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

ProgressSpeaker = Callable[[str], Awaitable[None]]


class ConversationPacer:
    """Emit at most two safe progress phrases while a turn is still running.

    The caller only supplies a coroutine that can speak a phrase.  This module
    owns timing, de-duplication, and cancellation so planners and tool adapters
    do not each invent their own filler behaviour.  It never describes a
    successful side effect; final responses remain the execution layer's job.
    """

    def __init__(
        self,
        speaker: ProgressSpeaker,
        *,
        initial_delay_seconds: float = 1.5,
        follow_up_delay_seconds: float = 4.0,
        initial_phrase: str = "One moment, I'm looking into that for you.",
        follow_up_phrase: str = "Thanks for your patience. I'm still checking.",
    ) -> None:
        if initial_delay_seconds < 0 or follow_up_delay_seconds < 0:
            raise ValueError("Pacing delays must be non-negative")
        self._speaker = speaker
        self._initial_delay_seconds = initial_delay_seconds
        self._follow_up_delay_seconds = follow_up_delay_seconds
        self._initial_phrase = initial_phrase
        self._follow_up_phrase = follow_up_phrase
        self._closed = False
        self._first_phrase_spoken = False
        self._follow_up_spoken = False
        self._lock = asyncio.Lock()
        self._initial_task: asyncio.Task[None] | None = None
        self._follow_up_task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Arm a generic acknowledgement for an otherwise silent slow turn."""

        if self._closed or self._initial_task is not None:
            return
        self._initial_task = asyncio.create_task(
            self._sleep_then_first_phrase(), name="hotel-conversation-pacer-initial"
        )

    async def offer(self, phrase: str) -> bool:
        """Offer a more specific phrase once the current operation is known."""

        normalized = " ".join(phrase.split())
        if not normalized:
            return False
        return await self._speak_first_phrase(normalized)

    async def close(self) -> None:
        """Cancel outstanding pacing before verified final speech begins."""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = [self._initial_task, self._follow_up_task]

        current_task = asyncio.current_task()
        cancellable = [
            task
            for task in tasks
            if task is not None and task is not current_task and not task.done()
        ]
        for task in cancellable:
            task.cancel()
        if cancellable:
            await asyncio.gather(*cancellable, return_exceptions=True)

    async def _sleep_then_first_phrase(self) -> None:
        try:
            await asyncio.sleep(self._initial_delay_seconds)
        except asyncio.CancelledError:
            return
        await self._speak_first_phrase(self._initial_phrase)

    async def _sleep_then_follow_up(self) -> None:
        try:
            await asyncio.sleep(self._follow_up_delay_seconds)
        except asyncio.CancelledError:
            return

        async with self._lock:
            if self._closed or self._follow_up_spoken:
                return
            self._follow_up_spoken = True
            await self._safely_speak(self._follow_up_phrase)

    async def _speak_first_phrase(self, phrase: str) -> bool:
        async with self._lock:
            if self._closed or self._first_phrase_spoken:
                return False
            self._first_phrase_spoken = True
            initial_task = self._initial_task
            if initial_task is not None and initial_task is not asyncio.current_task():
                initial_task.cancel()
            await self._safely_speak(phrase)
            if not self._closed:
                self._follow_up_task = asyncio.create_task(
                    self._sleep_then_follow_up(),
                    name="hotel-conversation-pacer-follow-up",
                )
            return True

    async def _safely_speak(self, phrase: str) -> None:
        try:
            await self._speaker(phrase)
        except Exception:
            # Pacing must never turn a successful hotel workflow into a failed
            # call.  The final validated response can still be spoken normally.
            logger.exception("Unable to speak hotel progress phrase")
