import pytest
from pydantic import ValidationError

from hoa_voice_agent.services.hotel_tools import (
    HOTEL_TOOL_DEFINITIONS,
    parse_tool_arguments,
)


def test_hotel_exposes_only_read_only_pms_tools() -> None:
    names = [item["function"]["name"] for item in HOTEL_TOOL_DEFINITIONS]

    assert names == [
        "list_room_types",
        "search_rooms",
        "get_room_details",
        "get_open_requests",
    ]


def test_room_details_requires_a_selector() -> None:
    with pytest.raises(ValidationError):
        parse_tool_arguments("get_room_details", {})


def test_search_rooms_validates_guest_count() -> None:
    with pytest.raises(ValidationError):
        parse_tool_arguments("search_rooms", {"guest_count": 0})


def test_search_rooms_requires_a_dated_party_query() -> None:
    with pytest.raises(ValidationError):
        parse_tool_arguments("search_rooms", {})


def test_search_rooms_rejects_a_human_date_before_it_reaches_the_pms() -> None:
    with pytest.raises(ValidationError):
        parse_tool_arguments(
            "search_rooms",
            {
                "check_in": "September 1",
                "check_out": "2026-09-18",
                "guest_count": 2,
            },
        )


def test_list_room_types_has_no_availability_arguments() -> None:
    parsed = parse_tool_arguments("list_room_types", {})

    assert parsed.model_dump() == {"response_scope": None}
