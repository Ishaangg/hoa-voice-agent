from hoa_voice_agent.services.hotel_booking_plan import (
    BookingPlan,
    composition_prompt,
)


def test_party_composition_creates_room_level_occupancies() -> None:
    plan = BookingPlan()

    changed = plan.apply_caller_turn(
        "We are two couples and one single, and we want separate rooms.",
        ["standard", "suite"],
    )

    assert changed is True
    assert plan.party_size == 5
    assert plan.occupancies == [2, 2, 1]
    assert plan.requested_room_count == 3


def test_explicit_room_type_replaces_unaccepted_recommendation_space() -> None:
    plan = BookingPlan(room_count=2, allow_mixed_types=True)

    plan.apply_caller_turn("Actually, I want 3 standard rooms.", ["standard", "suite"])

    assert plan.allow_mixed_types is False
    assert [(item.room_type, item.quantity) for item in plan.allocation] == [("standard", 3)]
    assert plan.requested_room_count == 3


def test_family_and_singles_are_preserved_as_distinct_room_groups() -> None:
    plan = BookingPlan()

    plan.apply_caller_turn("There is a family of three and two singles.", ["standard", "suite"])

    assert plan.party_size == 5
    assert plan.occupancies == [3, 1, 1]


def test_model_extraction_handles_open_ended_party_language() -> None:
    plan = BookingPlan()

    changed = plan.apply_model_update(
        party_groups=[
            {"label": "family", "people_per_group": 4, "count": 1},
            {"label": "single", "people_per_group": 1, "count": 2},
        ],
        allocation_mode="separate",
    )

    assert changed is True
    assert plan.party_size == 6
    assert plan.occupancies == [4, 1, 1]
    assert plan.requested_room_count == 3


def test_party_composition_can_be_explicitly_known_with_no_couples() -> None:
    plan = BookingPlan()

    changed = plan.apply_model_update(
        party_groups=[],
        party_composition_known=True,
    )

    assert changed is True
    assert plan.party_composition_known is True
    assert plan.party_groups == []
    assert BookingPlan.from_json(plan.to_json()).party_composition_known is True


def test_explicit_couples_default_to_separate_recommendation_groups() -> None:
    plan = BookingPlan()

    changed = plan.apply_model_update(
        party_groups=[{"label": "couple", "people_per_group": 2, "count": 3}],
        party_composition_known=True,
    )

    assert changed is True
    assert plan.party_composition_known is True
    assert plan.occupancies == [2, 2, 2]
    assert plan.allow_room_sharing is False


def test_composition_prompt_asks_only_for_couples() -> None:
    prompt = composition_prompt(6)

    assert "couples" in prompt
    assert "one room" not in prompt
    assert "separate rooms" not in prompt
