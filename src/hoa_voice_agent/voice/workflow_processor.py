from typing import Any
from uuid import UUID

from pipecat.frames.frames import Frame, LLMContextFrame, TTSSpeakFrame, TTSUpdateSettingsFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from hoa_voice_agent.services.conversation import ConversationService


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
        support_voice: str,
    ) -> None:
        super().__init__()
        self._service = service
        self._conversation_id = conversation_id
        self._support_voice = support_voice

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, LLMContextFrame):
            user_text = self._latest_user_text(frame.context.messages)
            if not user_text:
                return

            route = await self._service.route_voice_turn(self._conversation_id, user_text)
            if route.response:
                frame.context.add_message({"role": "assistant", "content": route.response.say})
                await self._service.record_assistant_turn(
                    self._conversation_id,
                    route.response.say,
                )
                await self._set_support_voice()
                await self.push_frame(TTSSpeakFrame(route.response.say), FrameDirection.DOWNSTREAM)
                return

            self._ensure_system_prompt(frame.context.messages, route.system_prompt)
            await self._set_support_voice()
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
            return
        messages.insert(0, {"role": "system", "content": prompt})

    async def _set_support_voice(self) -> None:
        await self.push_frame(
            TTSUpdateSettingsFrame(settings={"voice": self._support_voice}),
            FrameDirection.DOWNSTREAM,
        )
