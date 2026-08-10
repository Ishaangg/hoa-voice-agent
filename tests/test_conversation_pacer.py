import asyncio

import pytest

from hoa_voice_agent.voice.conversation_pacer import ConversationPacer


@pytest.mark.asyncio
async def test_pacer_speaks_generic_phrase_only_after_delay() -> None:
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    pacer = ConversationPacer(
        speak,
        initial_delay_seconds=0.01,
        follow_up_delay_seconds=1,
        initial_phrase="Let me check that for you.",
    )
    pacer.start()
    assert spoken == []

    await asyncio.sleep(0.02)
    await pacer.close()

    assert spoken == ["Let me check that for you."]


@pytest.mark.asyncio
async def test_specific_operation_phrase_beats_generic_timer() -> None:
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    pacer = ConversationPacer(
        speak,
        initial_delay_seconds=0.02,
        follow_up_delay_seconds=1,
        initial_phrase="Hmm, let me look into that.",
    )
    pacer.start()

    assert await pacer.offer("Let me pull up those room details for you.")
    await asyncio.sleep(0.03)
    await pacer.close()

    assert spoken == ["Let me pull up those room details for you."]


@pytest.mark.asyncio
async def test_pacer_adds_one_follow_up_for_a_still_slow_turn() -> None:
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    pacer = ConversationPacer(
        speak,
        initial_delay_seconds=1,
        follow_up_delay_seconds=0.01,
        follow_up_phrase="This is taking a moment. I'm still checking.",
    )
    assert await pacer.offer("Hmm, let me check the room options for those dates.")

    await asyncio.sleep(0.02)
    await pacer.close()

    assert spoken == [
        "Hmm, let me check the room options for those dates.",
        "This is taking a moment. I'm still checking.",
    ]


@pytest.mark.asyncio
async def test_pacer_close_cancels_pending_phrases() -> None:
    spoken: list[str] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    pacer = ConversationPacer(speak, initial_delay_seconds=0.01)
    pacer.start()
    await pacer.close()
    await asyncio.sleep(0.02)

    assert spoken == []
