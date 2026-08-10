"""Typed read-only function contracts for hotel PMS lookups."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ResponseScope = Literal[
    "catalogue",
    "feature_match",
    "price",
    "capacity",
    "room_details",
    "comparison",
    "recommendation",
    "recap",
]


class HotelToolInput(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class ListRoomTypesInput(HotelToolInput):
    """Request the hotel's configured room catalogue."""

    response_scope: ResponseScope | None = Field(
        default=None,
        description="Semantic answer scope for the caller's latest question.",
    )


class SearchRoomsInput(HotelToolInput):
    check_in: date = Field(description="Arrival date in ISO YYYY-MM-DD format.")
    check_out: date = Field(description="Departure date in ISO YYYY-MM-DD format.")
    guest_count: int = Field(ge=1, description="Number of guests.")
    guest_count_evidence: str | None = Field(
        default=None,
        description=(
            "Exact short span from the caller's transcript supporting guest_count. "
            "Leave empty when reusing persisted booking context."
        ),
    )
    room_type: str | None = Field(default=None, description="Requested room type, if specified.")
    required_features: list[str] = Field(
        default_factory=list,
        description="Verified catalogue feature keys the caller requires.",
    )
    preferred_features: list[str] = Field(
        default_factory=list,
        description="Verified catalogue feature keys the caller prefers.",
    )
    excluded_features: list[str] = Field(
        default_factory=list,
        description="Verified catalogue feature keys the caller does not want.",
    )
    removed_features: list[str] = Field(
        default_factory=list,
        description="Previously stated feature requirements the caller removed.",
    )
    max_nightly_rate_cents: int | None = Field(
        default=None,
        ge=0,
        description="Maximum acceptable nightly total for one recommended room, in cents.",
    )
    clear_max_nightly_rate: bool = Field(
        default=False,
        description="Remove a previously stated nightly budget limit.",
    )
    bed_type: str | None = Field(default=None, description="Requested bed type, if stated.")
    accessibility_required: bool | None = Field(
        default=None,
        description="Whether accessibility features are a hard requirement.",
    )
    criteria_evidence: dict[str, str] = Field(
        default_factory=dict,
        description="Short exact spans from the caller supporting each criteria change.",
    )
    response_scope: ResponseScope | None = Field(
        default=None,
        description="Semantic answer scope for the caller's latest question.",
    )


class GetRoomDetailsInput(HotelToolInput):
    room_id: str | None = Field(default=None, description="PMS room identifier.")
    room_type: str | None = Field(default=None, description="Room type identifier or name.")
    response_scope: ResponseScope | None = Field(
        default=None,
        description="Semantic answer scope for the caller's latest question.",
    )

    @model_validator(mode="after")
    def require_selector(self) -> GetRoomDetailsInput:
        if not self.room_id and not self.room_type:
            raise ValueError("Provide room_id or room_type")
        return self


class GetOpenRequestsInput(HotelToolInput):
    room_number: str | None = Field(
        default=None, description="Guest room to filter by, when known."
    )
    status: Literal["open", "assigned", "in_progress", "all"] = "open"
    limit: int = Field(default=10, ge=1, le=50)


TOOL_INPUT_MODELS: dict[str, type[HotelToolInput]] = {
    "list_room_types": ListRoomTypesInput,
    "search_rooms": SearchRoomsInput,
    "get_room_details": GetRoomDetailsInput,
    "get_open_requests": GetOpenRequestsInput,
}


def default_response_scope_for_tool(tool_name: str) -> ResponseScope:
    """Return a safe scope when a provider omits the semantic field."""

    return {
        "list_room_types": "catalogue",
        "search_rooms": "recommendation",
        "get_room_details": "room_details",
    }.get(tool_name, "room_details")


def _tool_definition(name: str, model: type[HotelToolInput], description: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": model.model_json_schema(),
        },
    }


HOTEL_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _tool_definition(
        "list_room_types",
        ListRoomTypesInput,
        "List the hotel's configured room types. This is a catalogue lookup "
        "only; it does not claim that rooms are available for particular dates.",
    ),
    _tool_definition(
        "search_rooms",
        SearchRoomsInput,
        "Find rooms available for the specified dates and number of guests. Use "
        "this only for a dated stay, not for general room-type options. Include "
        "caller-stated room requirements when they are present, using catalogue "
        "feature keys and exact transcript evidence. If the application context "
        "shows that party composition is still unknown for more than one guest, "
        "ask about couples before calling this tool.",
    ),
    _tool_definition(
        "get_room_details",
        GetRoomDetailsInput,
        "Get capacity, amenities metadata, and pricing for a room type.",
    ),
    _tool_definition(
        "get_open_requests",
        GetOpenRequestsInput,
        "List this guest's open or active hotel requests.",
    ),
]


def parse_tool_arguments[TToolInput: HotelToolInput](
    name: str,
    arguments: dict[str, Any],
    model: type[TToolInput] | None = None,
) -> TToolInput:
    """Validate decoded LLM arguments before the application executes a tool."""

    selected = model or TOOL_INPUT_MODELS.get(name)
    if selected is None:
        raise ValueError(f"Unknown hotel tool: {name}")
    return selected.model_validate(arguments)


__all__ = [
    "GetOpenRequestsInput",
    "GetRoomDetailsInput",
    "HOTEL_TOOL_DEFINITIONS",
    "ListRoomTypesInput",
    "SearchRoomsInput",
    "TOOL_INPUT_MODELS",
    "ResponseScope",
    "default_response_scope_for_tool",
    "parse_tool_arguments",
]
