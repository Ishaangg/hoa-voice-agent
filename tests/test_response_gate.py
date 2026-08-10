from pipecat.frames.frames import (
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    TTSSpeakFrame,
)

from hoa_voice_agent.voice.response_gate import ResponseTurnGate, VoiceOutputGateProcessor


def test_response_gate_accepts_same_words_as_separate_turns() -> None:
    gate = ResponseTurnGate()
    first = {"role": "user", "content": "hello"}
    second = {"role": "user", "content": "hello"}

    assert gate.accept_context([first])
    assert not gate.accept_context([first])
    assert gate.accept_context([first, {"role": "assistant", "content": "Hi"}, second])


def test_response_gate_keeps_stale_frames_idempotent_after_interruption() -> None:
    gate = ResponseTurnGate()
    user_turn = {"role": "user", "content": "what are the pool hours"}

    assert gate.accept_context([user_turn])
    assert gate.mark_interruption() == 1
    assert not gate.accept_context([user_turn])


def test_response_gate_is_bounded() -> None:
    gate = ResponseTurnGate(max_seen_turns=2)
    messages = [{"role": "user", "content": str(index)} for index in range(3)]

    for message in messages:
        assert gate.accept_context([message])
    assert gate.accept_context([messages[0]])


def test_output_gate_drops_late_llm_frames_after_interruption() -> None:
    gate = VoiceOutputGateProcessor()
    start = LLMFullResponseStartFrame()
    text = LLMTextFrame("old response")
    end = LLMFullResponseEndFrame()

    assert gate.should_forward(start)
    assert gate.should_forward(InterruptionFrame())
    assert not gate.should_forward(text)
    assert not gate.should_forward(end)


def test_output_gate_drops_stale_deterministic_speak_frame() -> None:
    gate = VoiceOutputGateProcessor()
    speak = TTSSpeakFrame("old response", append_to_context=False)
    speak.metadata[gate._EPOCH_METADATA_KEY] = gate.epoch

    assert gate.should_forward(speak)
    gate.mark_interruption()
    assert not gate.should_forward(speak)


def test_output_gate_allows_a_new_llm_response_after_interruption() -> None:
    gate = VoiceOutputGateProcessor()
    assert gate.should_forward(LLMFullResponseStartFrame())
    assert gate.should_forward(InterruptionFrame())
    assert gate.should_forward(LLMFullResponseStartFrame())
    assert gate.should_forward(LLMTextFrame("new response"))
    assert gate.should_forward(LLMFullResponseEndFrame())
