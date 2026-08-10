import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pipecat.frames.frames import LLMContextFrame, LLMTextFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection

from hoa_voice_agent.services.hotel_conversation import HotelTurnResponse, HotelVoiceRoute
from hoa_voice_agent.voice.hotel_workflow_processor import HotelWorkflowProcessor, _phrase_chunks


class _NoToolHotelService:
    """A normal conversation response that deliberately performs no PMS action."""

    def __init__(self) -> None:
        self.recorded_turns: list[str] = []

    async def route_voice_turn(self, conversation_id, text, *, on_response):
        assert text == "Am I audible?"
        await asyncio.sleep(0)
        return HotelVoiceRoute(
            response=HotelTurnResponse(
                conversation_id=conversation_id,
                intent="general",
                say="Yes, I can hear you clearly.",
            )
        )

    async def record_assistant_turn(self, _conversation_id, _text) -> None:
        self.recorded_turns.append(_text)


class _LookupHotelService:
    """A PMS lookup reports progress immediately before the lookup runs."""

    async def route_voice_turn(self, conversation_id, text, *, on_response):
        assert text == "What rooms are available?"
        assert await on_response("Let me check the available room options for you.")
        return HotelVoiceRoute(
            response=HotelTurnResponse(
                conversation_id=conversation_id,
                intent="booking",
                say="Two suite rooms are available.",
            )
        )

    async def record_assistant_turn(self, _conversation_id, _text) -> None:
        return None


@pytest.mark.asyncio
async def test_fast_non_tool_turn_cancels_generic_progress_phrase() -> None:
    """Pacing is armed early but does not speak when the model is fast."""

    conversation_id = uuid4()
    service = _NoToolHotelService()
    processor = HotelWorkflowProcessor(service, conversation_id)
    processor.push_frame = AsyncMock()
    context = LLMContext(messages=[{"role": "user", "content": "Am I audible?"}])

    await processor.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)

    spoken = "".join(
        call.args[0].text
        for call in processor.push_frame.await_args_list
        if isinstance(call.args[0], LLMTextFrame)
    )
    assert "One moment, I'm looking into that for you." not in spoken
    assert "Yes, I can hear you clearly." in spoken
    assert service.recorded_turns == []


async def test_non_tool_turn_does_not_arm_generic_progress_pacing(monkeypatch) -> None:
    started = False

    class SpyPacer:
        def __init__(self, _speaker, **_kwargs):
            return None

        def start(self) -> None:
            nonlocal started
            started = True

        async def offer(self, _text: str) -> bool:
            return False

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "hoa_voice_agent.voice.hotel_workflow_processor.ConversationPacer",
        SpyPacer,
    )
    processor = HotelWorkflowProcessor(_NoToolHotelService(), uuid4())
    processor.push_frame = AsyncMock()
    context = LLMContext(messages=[{"role": "user", "content": "Am I audible?"}])

    await processor.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)

    assert started is False


@pytest.mark.asyncio
async def test_lookup_turn_still_speaks_its_specific_progress_phrase() -> None:
    conversation_id = uuid4()
    processor = HotelWorkflowProcessor(_LookupHotelService(), conversation_id)
    processor.push_frame = AsyncMock()
    context = LLMContext(messages=[{"role": "user", "content": "What rooms are available?"}])

    await processor.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)

    spoken = "".join(
        call.args[0].text
        for call in processor.push_frame.await_args_list
        if isinstance(call.args[0], LLMTextFrame)
    )
    assert "Let me check the available room options for you." in spoken
    assert "Two suite rooms are available." in spoken


def test_phrase_chunks_insert_a_boundary_after_punctuation() -> None:
    assert _phrase_chunks("Samajh gaya?Saath hi, standard room bhi available hai.") == [
        "Samajh gaya?",
        "Saath hi, standard room bhi available hai.",
    ]


async def test_processor_passes_selected_language_to_generic_pacing(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class SpyPacer:
        def __init__(self, _speaker, **kwargs):
            captured.update(kwargs)

        def start(self) -> None:
            return None

        async def offer(self, _text: str) -> bool:
            return False

        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "hoa_voice_agent.voice.hotel_workflow_processor.ConversationPacer",
        SpyPacer,
    )
    processor = HotelWorkflowProcessor(
        _NoToolHotelService(),
        uuid4(),
        initial_language="hinglish",
    )
    processor.push_frame = AsyncMock()
    context = LLMContext(messages=[{"role": "user", "content": "Am I audible?"}])

    await processor.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)

    assert captured["initial_phrase"] == "Main abhi aapke liye check karti hoon."
    assert captured["follow_up_phrase"] == "Main isko abhi dekh leti hoon."
