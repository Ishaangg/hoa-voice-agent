from hoa_voice_agent.voice.interruption_gate import GuardedInterruptionUserTurnStartStrategy


def test_interruption_gate_recognizes_short_control_and_correction_phrases() -> None:
    assert GuardedInterruptionUserTurnStartStrategy.is_strong_interruption("stop")
    assert GuardedInterruptionUserTurnStartStrategy.is_strong_interruption("Actually")
    assert GuardedInterruptionUserTurnStartStrategy.is_strong_interruption("not my room")
    assert not GuardedInterruptionUserTurnStartStrategy.is_strong_interruption("okay")


def test_interruption_gate_requires_multiple_words_for_backchannel_barge_in() -> None:
    strategy = GuardedInterruptionUserTurnStartStrategy(min_words_while_bot_speaking=2)
    assert strategy._min_words_while_bot_speaking == 2
