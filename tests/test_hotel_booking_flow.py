from datetime import date

from hoa_voice_agent.services.hotel_booking_flow import (
    BookingLifecycleSnapshot,
    BookingSlotUpdate,
    BookingStateSnapshot,
    HotelBookingFlow,
    explicit_guest_count_from_text,
    validate_guest_count_evidence,
)


def _state(**overrides) -> BookingStateSnapshot:
    values = {
        "check_in": None,
        "check_out": None,
        "guest_count": None,
        "pending_category": None,
        "pending_description": None,
        "has_offer": False,
        "has_hold": False,
    }
    values.update(overrides)
    return BookingStateSnapshot(**values)


def test_guest_count_requires_caller_evidence() -> None:
    assert explicit_guest_count_from_text("Mujhe room for two guests chahiye.") == 2
    assert explicit_guest_count_from_text("Mujhe do logon ke liye room chahiye.") == 2
    assert explicit_guest_count_from_text("Kitne guests ke liye booking hai?") is None
    assert explicit_guest_count_from_text("I need a standard room.") is None


def test_guest_count_evidence_accepts_natural_hinglish_without_keyword_matching() -> None:
    result = validate_guest_count_evidence(
        "Mujhe चार लोगों के लिए room chahiye.",
        guest_count=4,
        evidence="चार लोगों के लिए",
    )

    assert result.guest_count == 4
    assert result.evidence == "चार लोगों के लिए"
    assert result.status == "explicit"


def test_guest_count_evidence_rejects_model_value_without_transcript_evidence() -> None:
    result = validate_guest_count_evidence(
        "Mujhe 14 August se 17 August tak room chahiye.",
        guest_count=1,
        evidence=None,
    )

    assert result.guest_count is None
    assert result.status == "unknown"


def test_guest_count_evidence_keeps_semantic_candidate_for_confirmation() -> None:
    result = validate_guest_count_evidence(
        "Mujhe dono ke liye hotel book karna hai.",
        guest_count=2,
        evidence="do logon ke liye",
    )

    assert result.guest_count == 2
    assert result.status == "semantic"


def test_transition_calculates_checkout_from_nights_and_owns_description() -> None:
    transition = HotelBookingFlow.transition(
        _state(pending_category="booking", pending_description="I need a room"),
        BookingSlotUpdate(
            check_in=date(2026, 9, 17),
            guest_count=2,
            nights=2,
        ),
        booking_intent=True,
        caller_text="for two guests",
        lookup_completed_this_turn=False,
    )

    assert transition.active is True
    assert transition.changed_slots is True
    assert transition.next_check_out == date(2026, 9, 19)
    assert transition.next_pending_category == "booking"
    assert transition.next_pending_description == "I need a room for two guests"


def test_transition_reuses_a_previously_captured_nights_value() -> None:
    transition = HotelBookingFlow.transition(
        _state(
            pending_category="booking",
            check_in=None,
            nights=1,
        ),
        BookingSlotUpdate(check_in=date(2026, 9, 17)),
        booking_intent=True,
        caller_text="check in on September 17",
        lookup_completed_this_turn=False,
    )

    assert transition.next_check_out == date(2026, 9, 18)
    assert transition.next_nights == 1


def test_transition_rejects_checkout_before_checkin_without_mutating_state() -> None:
    transition = HotelBookingFlow.transition(
        _state(pending_category="booking", check_in=date(2026, 9, 17)),
        BookingSlotUpdate(check_out=date(2026, 9, 16)),
        booking_intent=True,
        caller_text="check out on September 16",
        lookup_completed_this_turn=False,
    )

    assert transition.active is True
    assert transition.invalid_dates is True
    assert transition.reason == "checkout_must_follow_checkin"


def test_changed_slots_invalidate_existing_offer_or_hold() -> None:
    transition = HotelBookingFlow.transition(
        _state(
            check_in=date(2026, 9, 17),
            check_out=date(2026, 9, 19),
            guest_count=2,
            pending_category="booking",
            has_offer=True,
        ),
        BookingSlotUpdate(guest_count=5),
        booking_intent=True,
        caller_text="actually five guests",
        lookup_completed_this_turn=False,
    )

    assert transition.changed_slots is True
    assert transition.invalidate_offer is True


def test_comparison_plan_uses_owned_dates_and_drops_room_type_filter() -> None:
    plan = HotelBookingFlow.availability_plan(
        check_in=date(2026, 9, 17),
        check_out=date(2026, 9, 19),
        guest_count=2,
        room_type=None,
        purpose="comparison",
    )

    assert plan is not None
    assert plan.name == "search_rooms"
    assert plan.purpose == "comparison"
    assert plan.arguments == {
        "check_in": "2026-09-17",
        "check_out": "2026-09-19",
        "guest_count": 2,
    }


def test_acceptance_is_allowed_only_for_the_active_offer_prompt() -> None:
    decision = HotelBookingFlow.acceptance_decision(
        BookingLifecycleSnapshot(
            pending_category="booking",
            state="awaiting_booking_confirmation",
            has_offer=True,
            has_hold=False,
            has_reservation=False,
        ),
        affirmative=True,
    )

    assert decision.action == "accept_offer"
    assert decision.next_state == "awaiting_booking_details"

    rejected = HotelBookingFlow.acceptance_decision(
        BookingLifecycleSnapshot(
            pending_category="booking",
            state="collecting_booking_allocation",
            has_offer=True,
            has_hold=False,
            has_reservation=False,
        ),
        affirmative=True,
    )
    assert rejected.action == "none"


def test_confirmation_requires_a_live_hold_and_caller_name() -> None:
    decision = HotelBookingFlow.confirmation_decision(
        BookingLifecycleSnapshot(
            pending_category="booking",
            state="awaiting_booking_details",
            has_offer=True,
            has_hold=True,
            has_reservation=False,
        ),
        caller_name_provided=True,
    )
    assert decision.action == "confirm_hold"
    assert decision.next_state == "awaiting_post_action_followup"

    missing_hold = HotelBookingFlow.confirmation_decision(
        BookingLifecycleSnapshot(
            pending_category="booking",
            state="awaiting_booking_details",
            has_offer=True,
            has_hold=False,
            has_reservation=False,
        ),
        caller_name_provided=True,
    )
    assert missing_hold.action == "hold_missing"
    assert missing_hold.next_state == "collecting_booking_allocation"


def test_cancellation_clears_an_unfinished_booking_but_not_a_confirmed_reservation() -> None:
    decision = HotelBookingFlow.cancellation_decision(
        BookingLifecycleSnapshot(
            pending_category="booking",
            state="awaiting_booking_details",
            has_offer=True,
            has_hold=True,
            has_reservation=False,
        ),
        requested=True,
    )
    assert decision.action == "cancel_booking"
    assert decision.next_state == "ready"

    confirmed = HotelBookingFlow.cancellation_decision(
        BookingLifecycleSnapshot(
            pending_category="booking",
            state="awaiting_post_action_followup",
            has_offer=False,
            has_hold=False,
            has_reservation=True,
        ),
        requested=True,
    )
    assert confirmed.action == "none"
