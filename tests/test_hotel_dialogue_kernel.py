from hoa_voice_agent.services.hotel_dialogue_kernel import (
    HotelDialogueController,
    select_turn_response,
)
from hoa_voice_agent.services.hotel_turn_interpreter import HotelTurnInterpretation


def test_question_about_proposed_layout_cannot_change_booking_plan() -> None:
    interpretation = HotelTurnInterpretation(
        intent="booking",
        action="answer",
        dialogue_act="ask",
        explicit_selection=False,
        booking_plan_json=(
            '{"room_allocation":[{"room_type":"suite","quantity":2}]}'
        ),
    )

    decision = HotelDialogueController().assess_turn(
        interpretation,
        has_search_evidence=True,
    )

    assert decision.act == "ask"
    assert decision.caller_selected_option is False
    assert decision.can_update_booking_plan is False


def test_explicit_layout_selection_can_update_booking_plan() -> None:
    interpretation = HotelTurnInterpretation(
        intent="booking",
        action="answer",
        dialogue_act="choose",
        explicit_selection=True,
        booking_plan_json=(
            '{"room_allocation":[{"room_type":"standard","quantity":2}]}'
        ),
    )

    decision = HotelDialogueController().assess_turn(
        interpretation,
    )

    assert decision.caller_selected_option is True
    assert decision.can_update_booking_plan is True


def test_comparison_turn_requires_fresh_availability_evidence() -> None:
    interpretation = HotelTurnInterpretation(
        intent="booking",
        action="answer",
        dialogue_act="compare",
    )

    decision = HotelDialogueController().assess_turn(
        interpretation,
        has_search_evidence=True,
    )

    assert decision.act == "compare"
    assert decision.should_refresh_availability is True
    assert decision.can_update_booking_plan is False


def test_response_selection_does_not_replay_stale_pms_speech() -> None:
    interpretation = HotelTurnInterpretation(
        intent="booking",
        action="answer",
        response="No, those are two suites in total.",
    )

    assert select_turn_response(
        interpretation=interpretation,
        verified_response="For September 17 through September 19, 2 suite rooms fit your stay.",
        verified_evidence_is_current=False,
    ) == "No, those are two suites in total."
