from contextlib import suppress
from typing import Any
from uuid import UUID

from loguru import logger
from pipecat.frames.frames import Frame, InterruptionFrame, LLMContextFrame, TTSSpeakFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from hoa_voice_agent.services.conversation import ConversationService
from hoa_voice_agent.services.telemetry import CallTelemetry
from hoa_voice_agent.voice.language import VoiceLanguage
from hoa_voice_agent.voice.response_gate import ResponseTurnGate


class HOAWorkflowProcessor(FrameProcessor):
    """Turns a committed Pipecat user turn into a safe HOA workflow response.

    The processor receives an ``LLMContextFrame`` only after Pipecat's user-turn
    aggregation has committed a turn. Safety-sensitive maintenance turns stay in
    ``EmergencyPolicyEngine``; ordinary turns are passed to the streaming LLM downstream.
    """

    def __init__(
        self,
        service: ConversationService,
        conversation_id: UUID,
        initial_language: VoiceLanguage = "en",
        telemetry: CallTelemetry | None = None,
    ) -> None:
        super().__init__()
        self._service = service
        self._conversation_id = conversation_id
        # This value is supplied when the session is created and deliberately
        # never inferred from a transcript during the call.
        self._language = initial_language
        self._telemetry = telemetry
        self._response_gate = ResponseTurnGate()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InterruptionFrame):
            # Pipecat's system-frame path cancels the in-flight LLM/TTS tasks.
            # Keep the application idempotency state aligned with that epoch so
            # a late provider frame cannot be treated as a fresh response.
            epoch = self._response_gate.mark_interruption()
            logger.debug("HOA voice response epoch advanced to {}", epoch)
            await super().process_frame(frame, direction)
            await self.push_frame(frame, direction)
            return

        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            if not self._response_gate.accept_context(frame.context.messages):
                return
            user_text = self._latest_user_text(frame.context.messages)
            if not user_text:
                return

            try:
                route = await self._service.route_voice_turn(
                    self._conversation_id,
                    user_text,
                    language=self._language,
                )
            except Exception:
                # A database/network failure must not leave this long-lived
                # call session in SQLAlchemy's pending-rollback state. Keep
                # the call usable while logging the original exception.
                with suppress(Exception):
                    await self._service.session.rollback()
                logger.exception(
                    "HOA workflow failed for conversation {}; session rolled back",
                    self._conversation_id,
                )
                if self._telemetry:
                    self._telemetry.record("fallback", 1, metadata={"stage": "workflow"})
                fallback = "I'm sorry, I couldn't save that request. Please try again."
                frame.context.add_message({"role": "assistant", "content": fallback})
                with suppress(Exception):
                    await self._service.record_assistant_turn(
                        self._conversation_id,
                        fallback,
                    )
                await self.push_frame(
                    self._speak_frame(fallback),
                    FrameDirection.DOWNSTREAM,
                )
                return
            if route.response:
                if self._telemetry:
                    self._telemetry.record(
                        "task_success",
                        1,
                        metadata={"intent": route.response.intent, "mode": "workflow"},
                    )
                    if route.response.intent in {"identity", "correction"}:
                        self._telemetry.record("correction", 1)
                frame.context.add_message({"role": "assistant", "content": route.response.say})
                await self._service.record_assistant_turn(
                    self._conversation_id,
                    route.response.say,
                )
                await self.push_frame(
                    self._speak_frame(route.response.say),
                    FrameDirection.DOWNSTREAM,
                )
                return

            self._ensure_system_prompt(frame.context.messages, route.system_prompt)
            if self._telemetry:
                self._telemetry.record("task_success", 1, metadata={"mode": "llm"})
            await self.push_frame(frame, FrameDirection.DOWNSTREAM)
            return

        await self.push_frame(frame, direction)

    @staticmethod
    def _latest_user_text(messages: list[Any]) -> str | None:
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
        return None

    @staticmethod
    def _ensure_system_prompt(messages: list[Any], prompt: str | None) -> None:
        if not prompt:
            return
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            messages[0]["content"] = prompt
        else:
            messages.insert(0, {"role": "system", "content": prompt})

    def _speak_frame(self, text: str) -> TTSSpeakFrame:
        """Create a standalone response without duplicating context history."""

        frame = TTSSpeakFrame(text, append_to_context=False)
        frame.metadata["voice_response_epoch"] = self._response_gate.interruption_epoch
        return frame
