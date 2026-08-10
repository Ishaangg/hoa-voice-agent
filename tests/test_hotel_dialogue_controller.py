from hoa_voice_agent.services.hotel_dialogue_kernel import (
    DialogueSnapshot,
    HotelDialogueController,
    SemanticCommand,
)
from hoa_voice_agent.services.hotel_turn_interpreter import HotelTurnInterpretation


def test_controller_accepts_only_an_active_offer() -> None:
    controller = HotelDialogueController()
    interpretation = HotelTurnInterpretation(
        command="accept_offer",
        dialogue_act="accept",
        action="confirm",
    )

    decision = controller.decide_turn(
        DialogueSnapshot(
            state="awaiting_booking_confirmation",
            pending_category="booking",
            has_active_offer=True,
        ),
        interpretation,
    )

    assert decision.action == "accept_offer"
    assert decision.mutates_booking_state is False


def test_controller_does_not_turn_bare_acceptance_into_a_booking() -> None:
    controller = HotelDialogueController()
    interpretation = HotelTurnInterpretation(
        command="accept_offer",
        dialogue_act="accept",
        action="confirm",
    )

    decision = controller.decide_turn(
        DialogueSnapshot(state="collecting_booking_allocation"),
        interpretation,
    )

    assert decision.action == "clarify"
    assert decision.reason == "acceptance_without_pending_confirmation"


def test_controller_routes_acceptance_to_pending_fact_confirmation_first() -> None:
    controller = HotelDialogueController()
    interpretation = HotelTurnInterpretation(
        command="accept_offer",
        dialogue_act="accept",
        action="confirm",
    )

    decision = controller.decide_turn(
        DialogueSnapshot(
            state="collecting_booking_allocation",
            pending_category="booking",
            has_pending_fact_confirmation=True,
        ),
        interpretation,
    )

    assert decision.action == "accept_pending_fact"
    assert decision.mutates_booking_state is True


def test_controller_routes_rejection_to_pending_fact_confirmation_first() -> None:
    controller = HotelDialogueController()
    interpretation = HotelTurnInterpretation(
        command="reject_offer",
        dialogue_act="reject",
        action="none",
    )

    decision = controller.decide_turn(
        DialogueSnapshot(
            state="collecting_booking_allocation",
            pending_category="booking",
            has_pending_fact_confirmation=True,
        ),
        interpretation,
    )

    assert decision.action == "reject_pending_fact"
    assert decision.mutates_booking_state is True


def test_controller_answers_offer_questions_from_state_without_refreshing() -> None:
    controller = HotelDialogueController()
    interpretation = HotelTurnInterpretation(
        command="ask_about_state",
        command_target="party_composition",
        dialogue_act="ask",
        action="answer",
    )

    decision = controller.decide_turn(
        DialogueSnapshot(
            state="awaiting_booking_confirmation",
            pending_category="booking",
            has_active_offer=True,
            has_search_evidence=True,
        ),
        interpretation,
    )

    assert decision.action == "answer_from_state"
    assert decision.requires_tool is False
    assert decision.mutates_booking_state is False
    assert decision.accepts_fact_candidates is False


def test_controller_routes_catalogue_question_to_verified_read_tool() -> None:
    controller = HotelDialogueController()
    interpretation = HotelTurnInterpretation(
        command="ask_about_state",
        command_target="catalogue",
        dialogue_act="ask",
        action="answer",
    )

    decision = controller.decide_turn(DialogueSnapshot(), interpretation)

    assert decision.action == "answer_from_state"
    assert decision.requires_tool is True
    assert decision.tool_name == "list_room_types"


def test_controller_requests_availability_only_when_required_facts_exist() -> None:
    controller = HotelDialogueController()
    interpretation = HotelTurnInterpretation(
        command="request_recommendation",
        dialogue_act="compare",
        action="answer",
    )

    missing = controller.decide_turn(
        DialogueSnapshot(has_booking_dates=False, has_guest_count=True),
        interpretation,
    )
    ready = controller.decide_turn(
        DialogueSnapshot(has_booking_dates=True, has_guest_count=True),
        interpretation,
    )

    assert missing.action == "clarify"
    assert ready.action == "request_recommendation"
    assert ready.requires_tool is True
    assert ready.accepts_fact_candidates is True


def test_legacy_interpretation_is_adapted_to_semantic_command() -> None:
    interpretation = HotelTurnInterpretation(
        command="unknown",
        dialogue_act="accept",
        action="confirm",
    )

    command = SemanticCommand.from_interpretation(interpretation)

    assert command.kind == "accept_offer"
