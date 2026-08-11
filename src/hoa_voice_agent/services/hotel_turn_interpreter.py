"""LLM interpretation and read-only PMS tool calling for hotel turns."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import date
from typing import Any, Literal, Protocol

import httpx
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hoa_voice_agent.services.hotel_booking_flow import (
    validate_guest_count_evidence,
)
from hoa_voice_agent.services.hotel_tools import (
    HOTEL_TOOL_DEFINITIONS,
    ResponseScope,
    default_response_scope_for_tool,
    parse_tool_arguments,
)
from hoa_voice_agent.voice.language import language_adapter

HotelAuditEvent = Callable[[str, dict[str, Any]], Awaitable[None]]
ResponseLanguage = Literal["en", "hi", "hinglish"]
_ROOM_LOOKUP_TOOLS = frozenset(
    {"list_room_types", "search_rooms", "get_room_details"}
)


class _MissingCallerFactError(ValueError):
    """Raised when a required tool fact was not stated by the caller."""


DialogueAct = Literal[
    "inform",
    "ask",
    "choose",
    "accept",
    "reject",
    "correct",
    "compare",
    "cancel",
    "backchannel",
    "uncertain",
    "unknown",
    "other",
]

DialogueCommand = Literal[
    "update_facts",
    "ask_about_state",
    "request_recommendation",
    "select_option",
    "accept_offer",
    "reject_offer",
    "cancel",
    "inform",
    "unknown",
]


class HotelTurnInterpretation(BaseModel):
    """The language decision returned by the hotel model."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    intent: Literal[
        "identity_provided",
        "identity_correction",
        "identity_question",
        "room_service",
        "housekeeping",
        "maintenance",
        "security",
        "concierge",
        "faq",
        "confirmation",
        "cancellation",
        "booking",
        "general",
        "unknown",
    ] = "unknown"
    action: Literal[
        "answer",
        "ask_slot",
        "clarify",
        "confirm",
        "create_request",
        "cancel",
        "amend_request",
        "backchannel",
        "none",
    ] = "none"
    speech_status: Literal["complete", "incomplete", "backchannel", "uncertain"] = "complete"
    response_scope: ResponseScope = "room_details"
    # This is a semantic act, not a database command.  The application uses
    # it to distinguish a question about a proposed room from an explicit
    # selection of that room.
    dialogue_act: DialogueAct = "unknown"
    # Semantic meaning for the application controller. This is not a database
    # command and cannot authorize a tool or booking side effect.
    command: DialogueCommand = "unknown"
    command_target: str | None = None
    explicit_selection: bool = False
    name: str | None = None
    room_number: str | None = None
    service_category: Literal[
        "room_service",
        "housekeeping",
        "maintenance",
        "security",
        "concierge",
    ] | None = None
    service_description: str | None = None
    target_request_id: str | None = None
    follow_up_required: bool = False
    follow_up_question: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    response: str | None = None
    booking_check_in: str | None = None
    booking_check_out: str | None = None
    booking_guest_count: int | None = Field(default=None, ge=1)
    booking_guest_count_evidence: str | None = None
    booking_nights: int | None = Field(default=None, ge=1)
    booking_room_preference: str | None = None
    booking_plan_json: str | None = None
    booking_required_features: list[str] = Field(default_factory=list)
    booking_preferred_features: list[str] = Field(default_factory=list)
    booking_excluded_features: list[str] = Field(default_factory=list)
    booking_removed_features: list[str] = Field(default_factory=list)
    booking_max_nightly_rate_cents: int | None = Field(default=None, ge=0)
    booking_clear_max_nightly_rate: bool = False
    booking_bed_type: str | None = None
    booking_accessibility_required: bool | None = None
    booking_criteria_evidence: dict[str, str] = Field(default_factory=dict)
    read_only_tools: tuple[str, ...] = Field(default=(), exclude=True)
    tool_error: str | None = Field(default=None, exclude=True)
    # Raw tool results are application evidence, never model output. They are
    # kept out of persistence/serialization so the conversation service can
    # render verified facts without asking the LLM to summarize them again.
    tool_results: dict[str, Any] = Field(default_factory=dict, exclude=True)


class HotelTurnInterpreter(Protocol):
    async def interpret(
        self,
        *,
        text: str,
        last_assistant_turn: str | None,
        caller_name: str | None,
        room_number: str | None,
        pending_category: str | None,
        pending_description: str | None,
        active_topic: str | None,
        last_intent: str | None,
        response_language: ResponseLanguage = "en",
        approved_facts: dict[str, str] | None = None,
        request_history: list[dict[str, str | None]] | None = None,
        on_response: Callable[[str], Awaitable[None]] | None = None,
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        audit_event: HotelAuditEvent | None = None,
        booking_check_in: str | None = None,
        booking_check_out: str | None = None,
        booking_guest_count: int | None = None,
        booking_nights: int | None = None,
        booking_room_preference: str | None = None,
        booking_facts_json: str | None = None,
        booking_pending_fact_confirmation: bool = False,
        booking_plan_json: str | None = None,
        booking_last_search_result: str | None = None,
        booking_criteria_json: str | None = None,
        booking_catalog_snapshot_version: int | None = None,
        booking_catalog_relevant_json: str | None = None,
    ) -> HotelTurnInterpretation | None:
        ...


_TOOL_COMPATIBILITY_SYSTEM_PROMPT = """You are the semantic interpreter for a hotel voice concierge.
Understand natural speech, speech-to-text errors, short answers, and incomplete
answers. Return a structured decision; never claim that a database write or
booking happened unless the application confirms it.

Return one JSON object with these keys:
intent, action, speech_status, name, room_number, service_category,
service_description, target_request_id, follow_up_required, follow_up_question,
confidence, dialogue_act, command, command_target, explicit_selection, response,
    response_scope, booking_check_in,
booking_check_out, booking_guest_count, booking_nights, booking_room_preference,
booking_guest_count_evidence, booking_plan_json, booking_required_features,
booking_preferred_features, booking_excluded_features, booking_removed_features,
booking_max_nightly_rate_cents, booking_bed_type, booking_accessibility_required,
booking_clear_max_nightly_rate, booking_criteria_evidence.

Allowed intents: identity_provided, identity_correction, identity_question,
room_service, housekeeping, maintenance, security, concierge, faq,
confirmation, cancellation, booking, general, unknown.
Allowed actions: answer, ask_slot, clarify, confirm, create_request, cancel,
amend_request, backchannel, none.
Allowed service_category values: room_service, housekeeping, maintenance,
security, concierge.

Allowed dialogue_act values: inform, ask, choose, accept, reject, correct,
compare, cancel, backchannel, uncertain, other. Set explicit_selection=true
only when the caller clearly selected a room or layout in this turn. A caller
asking whether an option works, asking for other options, asking for a
recommendation, or repeating a proposed option is not an explicit selection.

Allowed command values: update_facts, ask_about_state, request_recommendation,
select_option, accept_offer, reject_offer, cancel, inform, unknown. The command
is the caller's semantic meaning for the application controller. Use
ask_about_state when the caller asks about facts already held by the application
or about the current offer. Use accept_offer or reject_offer only when the
caller is answering an active confirmation. Use update_facts for new booking
information or a correction to an earlier value. Never encode a tool name,
database write, room allocation, or hold operation as a command.

Rules:
- Choose response_scope semantically from the caller's latest question. Allowed
  values are catalogue (room names only), feature_match (requested amenities),
  price, capacity, room_details, comparison, recommendation, and recap. Do not
  infer it by matching a fixed keyword list. When calling a room lookup tool,
  copy the same semantic scope into that tool's response_scope argument.
- The input context contains response_language. Write all caller-facing
  response and follow_up_question text in that
  language. For "hinglish", use natural Roman-script Hindi mixed with familiar
  English hotel terms, as a human Indian hotel concierge would speak. Do not
  translate structured enum values, ISO dates, room types, prices, IDs, or
  tool arguments.
- Treat guest_text as the caller's verbatim STT transcript. It may contain
  Hindi, English, Roman Hindi, Devanagari, or code-switching. Never translate
  guest_text into English before interpreting it, and never replace it with a
  normalized English paraphrase.
- Extract name and room_number only when the caller states them. Room numbers
  may be spoken as digits or words; return digits only.
- Use create_request only when the service and concrete description are clear.
  Use ask_slot or clarify when something necessary is missing.
- Use cancellation when the caller wants to cancel an existing request or hold.
- When the caller is answering a concrete offer or hold prompt, understand
  natural approval language in any supported language or code-switching style.
  Set dialogue_act=accept and action=confirm for meanings such as "okay",
  "haan", "haan ji", "जी", "हाँ", "theek hai", "ठीक है", "kar do",
  "please do", or "go ahead". A short "yes" or "जी" is an acceptance when
  it answers the immediately preceding confirmation prompt. Do not require
  an exact phrase. Set dialogue_act=reject for meanings such as "no", "no
  thanks", "nahi", "nahi ji", "नहीं", "नहीं जी", "cancel", or "not now"
  when they answer that prompt. Do not classify a question, hesitation, or
  unrelated backchannel as acceptance or rejection.
- Use booking for availability, reservations, dates, guests, room types, or
  recommendations. Dates must be ISO YYYY-MM-DD; resolve relative dates using
  the supplied current_date. booking_nights is an integer when stated.
- When the caller explicitly states a guest count, return the integer in
  booking_guest_count and copy a short, exact span from guest_text into
  booking_guest_count_evidence. Preserve the caller's original language and
  script; do not translate the evidence. If the caller did not state a count
  in this turn, leave booking_guest_count_evidence empty and do not invent a
  count.
- booking_plan_json is optional and may contain only caller-explicit layout
  facts in this exact shape: {"party_groups":[{"label":"couple",
  "people_per_group":2,"count":1}],"room_allocation":[{"room_type":"standard",
  "quantity":1}],"room_count":3,"room_count_evidence":"three rooms",
  "allocation_mode":"mixed|shared|separate",
  "party_composition_known":true}. Omit a key when the caller did not say it.
  When the caller explicitly gives only an untyped room quantity, return
  room_count and copy the exact transcript span into room_count_evidence. Do
  not infer room_count from guests, couples, capacity, or a recommendation.
  When the application asks how many couples are in a group, set
  party_composition_known=true and return party_groups for the couples. If the
  caller says there are no couples, return party_composition_known=true with
  party_groups=[]. Do not invent a number of couples or a room layout.
  Never put a PMS recommendation, an offered alternative, or an assumed room
  count in room_count or room_allocation.
- If the persisted booking plan already contains party_groups and
  allow_room_sharing=false, that non-sharing layout is already settled. Keep
  each described group together and let the application place ungrouped
  guests in the remaining rooms. Do not ask whether an ungrouped guest will
  share with a couple, and do not ask whether the separate room is for the
  couple or the individual.
- When more than one guest is known but party_composition_known is not true in
  the persisted booking plan, do not call search_rooms yet. Ask only whether
  there are couples and, if so, how many. The application will check the room
  split after that answer. Do not ask whether the whole group will use one room
  versus separate rooms as a generic step.
- If the caller asks a question about a previously proposed layout, set
  dialogue_act=ask or compare, explicit_selection=false, and leave
  booking_plan_json empty even if the caller repeats room names or quantities.
- For room requirements, use booking_required_features for hard requirements,
  booking_preferred_features for nice-to-haves, booking_excluded_features for
  things the caller does not want, and booking_removed_features when the caller
  withdraws an earlier requirement. Use the exact feature keys supplied by the
  room catalogue context; do not invent keys. Put a short exact span from
  guest_text in booking_criteria_evidence for each changed criterion.
- booking_catalog_relevant_json contains verified room metadata currently in
  conversation state. Use its amenity keys or names when setting criteria;
  never invent a feature from general hotel knowledge.
- Convert an explicitly stated nightly budget to integer cents in
  booking_max_nightly_rate_cents. Leave it empty when no budget was stated.
  Set booking_clear_max_nightly_rate=true when the caller removes an earlier
  budget limit.
  A changed requirement is not a room selection and must not populate
  booking_plan_json.
- Use response only for a concise natural response. Do not invent
  hotel facts, room availability, prices, request status, or confirmations.
  For those facts, use a read-only tool when one is available.
- Use list_room_types for a general catalogue/options question when the caller
  has not supplied a dated stay. It lists configured room types only; it does
  not claim date-specific availability.
- Use search_rooms only when check-in, check-out, and guest_count are known. It
  is the date-aware availability lookup, not the room catalogue.
- A guest count is known only when the caller stated it in guest_text or it is
  already present in persisted booking context. Never default it to 1, copy it
  from a recommendation, or infer it from a room type. If it is missing, ask
  for the number of guests and do not call search_rooms.
- booking_facts_json is an application-owned ledger. Active facts are usable;
  pending_confirmation facts are proposals only. If the caller confirms a
  correction, let the application commit it before using the new value.
- If the caller asks for other, different, or alternative room options after a
  previous search, classify dialogue_act as compare. If the dates and guest
  count are still known, call search_rooms again without room_type so the
  application can compare the complete current inventory. Do not repeat the
  previous availability sentence as the answer to a new comparison question.
- When search_rooms has requested_room_type_status="sold_out", you MUST state
  that exact requested_room_type is sold out for check_in through check_out.
  If alternative_room_types are present, offer only those alternatives and
  state each exact available_room_count. Ask whether the caller wants to
  consider one or check different dates; never silently replace the caller's
  room choice or claim a hold.
- For a normal availability result, when the requested room type is available
  or no room type was requested, name the available room types only. Do not
  state available_room_count or count the raw room rows. Mention inventory
  counts only when the caller explicitly asks for them, when exact counts are
  needed for sold-out alternatives, or when presenting a multi-room layout.
- When search_rooms has requested_room_type_status="does_not_fit_party", say
  that the requested type cannot accommodate guest_count for that stay, then
  offer only the listed alternatives. When it has no requested rooms or
  alternatives and no multi_room_recommendations, say there is no suitable
  room for those dates and invite the caller to try different dates. Do not
  invent availability.
- When search_rooms includes non-empty multi_room_recommendations, no single
  room fits guest_count but a dated multi-room layout does. State that plainly,
  name the available room types, recommend the first layout, and ask whether
  the caller wants that exact layout held. Do not jump to changing dates while
  a multi-room recommendation is available.
- Use get_room_details for room facts, and get_open_requests for request
  status. Never call a write tool; none are provided. The application owns all
  writes and side effects.
- After a read-only tool result, answer from that result. If the caller asked
  to hear the available room types, do not ask them to choose a room type
  before listing the types unless a specific selection is truly required.
- After any read-only tool result, use action="answer". A tool result is never
  authority to create, amend, cancel, confirm, or otherwise write hotel data.
- If a tool result says nothing was found, say so plainly and do not guess.
- For ordinary turns, final speech should be one or two short sentences.
"""


_COMPACT_SYSTEM_PROMPT = """You are a semantic parser for a hotel voice assistant.
Return exactly one JSON object; omit unknown fields. The application controls
database writes, holds, availability, prices, and replies.

Use only these fields when needed:
- intent: booking, identity_provided, identity_correction, identity_question,
  room_service, housekeeping, maintenance, security, concierge, faq,
  confirmation, cancellation, general, or unknown.
- action: answer, ask_slot, clarify, confirm, create_request, cancel,
  amend_request, backchannel, or none.
- dialogue_act: inform, ask, choose, accept, reject, correct, compare, cancel,
  backchannel, uncertain, other, or unknown.
- command: update_facts, ask_about_state, request_recommendation,
  select_option, accept_offer, reject_offer, cancel, inform, or unknown.
- action is the dialogue verb; command is the application meaning. New facts:
  action="answer", command="update_facts"; never put update_facts in action.
- name, room_number, service_category, service_description, and
  follow_up_question only when the caller actually supplied them.
- booking_check_in and booking_check_out as ISO dates; booking_nights; and
  booking_guest_count with an exact short booking_guest_count_evidence span
  copied from guest_text. Never infer a guest count.
- booking_room_preference only for a caller-selected room type.
- booking_plan_json only for caller-stated layout facts. It may be a JSON
  object with party_composition_known and party_groups. A couple is
  {"label":"couple","people_per_group":2,"count":N}. For an explicit
  untyped room quantity, include room_count and exact room_count_evidence;
  never turn it into an empty allocation. Never infer room allocation,
  prices, availability, or unstated groups.
- booking_required_features, booking_preferred_features,
  booking_excluded_features, booking_removed_features,
  booking_max_nightly_rate_cents, booking_bed_type,
  booking_accessibility_required, booking_criteria_evidence: caller-stated
  room requirements only; use feature keys present in context.

guest_text is verbatim STT; never translate it. expected_booking_slot=
party_composition means answer how many couples: return booking_plan_json with
party_composition_known and stated couple groups only. No couples means
party_groups=[] (informative, not rejection). "Around one couple" is one couple;
do not invent the remaining guests. If
booking_pending_fact_confirmation=true, a semantic yes is action="confirm",
dialogue_act="accept", command="accept_offer". Otherwise a bare acknowledgement
with no fact is action="backchannel", dialogue_act="backchannel".
Examples: pending confirmation + "\u0939\u093e\u0902 \u091c\u0940" -> confirm, accept,
accept_offer. No pending confirmation + "Okay" -> backchannel, backchannel.
"""


_COMPACT_RETRY_PROMPT = """Return exactly one minimal JSON object for the caller's
latest hotel turn. Omit unknown fields. Do not write prose, markdown, booking
claims, prices, availability, or a room allocation. Preserve caller-provided
facts only. If expected_booking_slot is party_composition, return
booking_plan_json with party_composition_known and stated couple groups; no
couples means party_groups=[] and an informative answer."""


def _tool_progress_key(tool_calls: list[dict[str, Any]]) -> str:
    names = {
        call.get("function", {}).get("name")
        for call in tool_calls
        if isinstance(call, dict) and isinstance(call.get("function"), dict)
    }
    for tool_name in ("list_room_types", "search_rooms", "get_room_details", "get_open_requests"):
        if tool_name in names:
            return tool_name
    return "default"


def _optional_int(value: Any, *, field_name: str) -> int | None:
    """Normalize an optional integer returned by the model or a tool call."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer when provided") from exc


def _optional_bool(value: Any) -> Any:
    """Normalize common provider representations of a nullable boolean."""

    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if not normalized:
            return None
        if normalized in {"true", "yes", "y", "1"}:
            return True
        if normalized in {"false", "no", "n", "0"}:
            return False
    return value


def _tool_progress_phrase(
    tool_calls: list[dict[str, Any]],
    *,
    phrase_index: int = 0,
    response_language: ResponseLanguage = "en",
) -> str:
    """Choose a truthful, varied acknowledgement for the selected tool."""

    adapter = language_adapter(
        response_language,
        english_voice="",
        hindi_voice="",
    )
    return adapter.tool_progress(
        _tool_progress_key(tool_calls),
        phrase_index=phrase_index,
    )


class OpenAIHotelTurnInterpreter:
    """OpenAI-compatible semantic parser.

    Tool execution is intentionally opt-in for compatibility.  The production
    hotel conversation service marks this interpreter as controller-routed, so
    this class parses meaning and the controller owns the PMS side effect.
    Direct callers may still pass ``tool_executor`` while the migration is in
    progress.
    """

    controller_owns_tools = True

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        reasoning_effort: str = "none",
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._client_factory = client_factory or (lambda: httpx.AsyncClient(timeout=8.0))
        self._progress_phrase_positions: dict[str, int] = {}

    _INTERNAL_OUTPUT_FIELDS = {"read_only_tools", "tool_error", "tool_results"}

    @classmethod
    def _structured_response_format(cls) -> dict[str, Any]:
        """Build the strict schema used for model-owned interpretation fields."""

        model_schema = deepcopy(HotelTurnInterpretation.model_json_schema())
        properties = {
            name: schema
            for name, schema in model_schema.get("properties", {}).items()
            if name not in cls._INTERNAL_OUTPUT_FIELDS
        }

        def remove_unsupported_metadata(value: Any) -> None:
            if isinstance(value, dict):
                for key in (
                    "default",
                    "description",
                    "maximum",
                    "minimum",
                    "title",
                ):
                    value.pop(key, None)
                for child in value.values():
                    remove_unsupported_metadata(child)
            elif isinstance(value, list):
                for child in value:
                    remove_unsupported_metadata(child)

        remove_unsupported_metadata(properties)
        schema = {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        }
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "hotel_turn_interpretation",
                "strict": True,
                "schema": schema,
            },
        }

    async def interpret(
        self,
        *,
        text: str,
        last_assistant_turn: str | None,
        caller_name: str | None,
        room_number: str | None,
        pending_category: str | None,
        pending_description: str | None,
        active_topic: str | None,
        last_intent: str | None,
        response_language: ResponseLanguage = "en",
        approved_facts: dict[str, str] | None = None,
        request_history: list[dict[str, str | None]] | None = None,
        on_response: Callable[[str], Awaitable[None]] | None = None,
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
        audit_event: HotelAuditEvent | None = None,
        booking_check_in: str | None = None,
        booking_check_out: str | None = None,
        booking_guest_count: int | None = None,
        booking_nights: int | None = None,
        booking_room_preference: str | None = None,
        booking_facts_json: str | None = None,
        booking_pending_fact_confirmation: bool = False,
        booking_plan_json: str | None = None,
        booking_last_search_result: str | None = None,
        booking_criteria_json: str | None = None,
        booking_catalog_snapshot_version: int | None = None,
        booking_catalog_relevant_json: str | None = None,
    ) -> HotelTurnInterpretation | None:
        context: dict[str, Any] = {
            "current_date": date.today().isoformat(),
            "guest_text": text,
            "response_language": response_language,
        }
        for key, value in (
            ("last_assistant_turn", last_assistant_turn),
            ("caller_name", caller_name),
            ("room_number", room_number),
            ("pending_category", pending_category),
            ("pending_description", pending_description),
            ("active_topic", active_topic),
            ("last_intent", last_intent),
            ("booking_check_in", booking_check_in),
            ("booking_check_out", booking_check_out),
            ("booking_guest_count", booking_guest_count),
            ("booking_nights", booking_nights),
            ("booking_room_preference", booking_room_preference),
            ("booking_facts_json", booking_facts_json),
            (
                "booking_pending_fact_confirmation",
                booking_pending_fact_confirmation,
            ),
            ("booking_plan_json", booking_plan_json),
            ("booking_last_search_result", booking_last_search_result),
            ("booking_criteria_json", booking_criteria_json),
            ("booking_catalog_snapshot_version", booking_catalog_snapshot_version),
            ("booking_catalog_relevant_json", booking_catalog_relevant_json),
        ):
            if value not in (None, ""):
                context[key] = value
        if approved_facts:
            context["approved_facts"] = approved_facts
        if request_history:
            context["request_history"] = request_history

        try:
            async with self._client_factory() as client:
                first_payload = self._payload(context, with_tools=tool_executor is not None)
                await self._emit_audit(
                    audit_event,
                    "llm_request",
                    {"phase": "initial", "payload": first_payload},
                )
                try:
                    first = await self._post_completion(
                        client,
                        first_payload,
                        audit_event=audit_event,
                        audit_phase="initial",
                    )
                    first_data = first.json()
                    if not tool_executor:
                        interpretation = self._parse_response(first_data)
                        if self._needs_semantic_slot_repair(interpretation, context):
                            repaired = await self._retry_compact_interpretation(
                                client,
                                context,
                                audit_event=audit_event,
                                initial_error=ValueError(
                                    "active booking slot was not semantically resolved"
                                ),
                            )
                            return repaired or interpretation
                        return interpretation
                except (
                    httpx.HTTPError,
                    KeyError,
                    TypeError,
                    ValueError,
                    ValidationError,
                    json.JSONDecodeError,
                ) as exc:
                    if tool_executor is not None:
                        raise
                    return await self._retry_compact_interpretation(
                        client,
                        context,
                        audit_event=audit_event,
                        initial_error=exc,
                    )

                message = (first_data.get("choices") or [{}])[0].get("message") or {}
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    return self._parse_response(first_data)

                prepared_calls: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
                for call in tool_calls:
                    function = call.get("function") or {}
                    name = function.get("name")
                    raw_arguments = function.get("arguments") or "{}"
                    arguments: dict[str, Any] = {}
                    try:
                        if not isinstance(name, str) or not name:
                            raise ValueError("Tool call did not include a name")
                        arguments = json.loads(raw_arguments)
                        if not isinstance(arguments, dict):
                            raise ValueError("Tool arguments must be a JSON object")
                        completed_arguments = self._complete_tool_arguments(
                            name,
                            arguments,
                            context,
                        )
                        validated = parse_tool_arguments(name, completed_arguments)
                    except _MissingCallerFactError:
                        await self._emit_audit(
                            audit_event,
                            "tool_call_blocked",
                            {
                                "tool": name,
                                "reason": "missing_caller_guest_count",
                            },
                        )
                        return self._missing_guest_count_response(
                            response_language,
                            arguments,
                        )
                    except (TypeError, ValueError, ValidationError, json.JSONDecodeError) as exc:
                        logger.warning("Hotel tool validation failed name={} error={}", name, exc)
                        return self._tool_failure(name)
                    prepared_calls.append(
                        (
                            call,
                            name,
                            validated.model_dump(
                                mode="json",
                                exclude_none=True,
                                exclude_defaults=True,
                            ),
                        )
                    )

                await self._emit_audit(
                    audit_event,
                    "validated_tool_calls",
                    {
                        "tools": [
                            {"name": name, "arguments": arguments}
                            for _, name, arguments in prepared_calls
                        ]
                    },
                )

                if on_response:
                    progress_key = _tool_progress_key(tool_calls)
                    phrase_index = self._progress_phrase_positions.get(progress_key, 0)
                    self._progress_phrase_positions[progress_key] = phrase_index + 1
                    await on_response(
                        _tool_progress_phrase(
                            tool_calls,
                            phrase_index=phrase_index,
                            response_language=response_language,
                        )
                    )

                tool_results: dict[str, Any] = {}
                tool_messages: list[dict[str, Any]] = [
                    {
                        "role": "assistant",
                        "content": message.get("content") or "",
                        "tool_calls": tool_calls,
                    }
                ]
                for call, name, arguments in prepared_calls:
                    try:
                        result = await tool_executor(name, arguments)
                    except Exception as exc:
                        logger.warning("Hotel tool execution failed name={} error={}", name, exc)
                        return self._tool_failure(name)
                    tool_results[name] = result
                    tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", "hotel_tool"),
                            "content": json.dumps(result, default=str),
                        }
                    )

                tool_names = tuple(name for _, name, _ in prepared_calls)
                if set(tool_names).issubset(_ROOM_LOOKUP_TOOLS):
                    # PMS facts are already authoritative. Do not spend a
                    # second model round trip asking the LLM to restate them;
                    # the conversation service renders them in the selected
                    # caller language.
                    interpretation = self._interpretation_from_tool_results(
                        context,
                        message,
                        prepared_calls,
                        tool_results,
                    )
                else:
                    final_payload = self._payload(context, with_tools=False)
                    final_payload["messages"] = final_payload["messages"][:1] + [
                        {"role": "user", "content": json.dumps(context)},
                        *tool_messages,
                    ]
                    await self._emit_audit(
                        audit_event,
                        "llm_request",
                        {"phase": "after_tools", "payload": final_payload},
                    )
                    final = await self._post_completion(
                        client,
                        final_payload,
                        audit_event=audit_event,
                        audit_phase="after_tools",
                    )
                    try:
                        interpretation = self._parse_response(final.json())
                    except (ValueError, ValidationError) as exc:
                        # Keep the repair only for tools that do not yet have
                        # an application-owned renderer. Room lookups never
                        # reach this branch.
                        logger.warning(
                            "Hotel interpreter returned invalid post-tool JSON; retrying: {}",
                            exc,
                        )
                        retry_payload = dict(final_payload)
                        retry_payload["messages"] = [
                            *final_payload["messages"],
                            {
                                "role": "user",
                                "content": (
                                    "Your previous response was not valid JSON. Return exactly "
                                    "one JSON object matching the requested schema, with no "
                                    "markdown or explanation outside the object."
                                ),
                            },
                        ]
                        await self._emit_audit(
                            audit_event,
                            "llm_request",
                            {"phase": "after_tools_json_repair", "payload": retry_payload},
                        )
                        repaired = await self._post_completion(
                            client,
                            retry_payload,
                            audit_event=audit_event,
                            audit_phase="after_tools_json_repair",
                        )
                        interpretation = self._parse_response(repaired.json())
                accepted_booking_evidence = next(
                    (
                        (arguments["guest_count"], arguments["guest_count_evidence"])
                        for _, name, arguments in prepared_calls
                        if name == "search_rooms"
                        and arguments.get("guest_count") is not None
                        and arguments.get("guest_count_evidence")
                    ),
                    None,
                )
                if accepted_booking_evidence:
                    interpretation = interpretation.model_copy(
                        update={
                            "booking_guest_count": accepted_booking_evidence[0],
                            "booking_guest_count_evidence": accepted_booking_evidence[1],
                        }
                    )
                return interpretation.model_copy(
                    update={
                        "action": "answer",
                        "read_only_tools": tool_names,
                        "tool_results": tool_results,
                    }
                )
        except httpx.HTTPStatusError as exc:
            detail = self._provider_error_detail(exc.response)
            logger.warning(
                "Hotel LLM provider rejected request status={} detail={}",
                exc.response.status_code,
                detail,
            )
            return None
        except (httpx.HTTPError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("Hotel LLM interpretation failed: {}", exc)
            return None

    async def _post_completion(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
        *,
        audit_event: HotelAuditEvent | None,
        audit_phase: str,
    ) -> httpx.Response:
        """Post once normally, with a compatibility retry only if needed.

        Strict Structured Outputs is the normal path. A provider that does not
        support ``json_schema`` can fall back to legacy JSON mode, but that
        compatibility branch is never used by a supported provider.
        """

        response = await client.post(
            f"{self._base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if not self._is_structured_output_rejection(payload, exc.response):
                raise
            legacy_payload = dict(payload)
            legacy_payload["response_format"] = {"type": "json_object"}
            await self._emit_audit(
                audit_event,
                "llm_request",
                {
                    "phase": f"{audit_phase}_legacy_json_fallback",
                    "payload": legacy_payload,
                },
            )
            logger.warning(
                "Hotel provider does not support strict Structured Outputs; "
                "using legacy JSON mode for this request"
            )
            response = await client.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=legacy_payload,
            )
            response.raise_for_status()
        return response

    @classmethod
    def _is_structured_output_rejection(
        cls,
        payload: dict[str, Any],
        response: httpx.Response,
    ) -> bool:
        response_format = payload.get("response_format")
        if not isinstance(response_format, dict) or response_format.get("type") != "json_schema":
            return False
        if response.status_code not in {400, 422}:
            return False
        detail = cls._provider_error_detail(response).casefold()
        return any(
            marker in detail
            for marker in (
                "json_schema",
                "structured output",
                "response_format",
                "unsupported schema",
                "schema is not supported",
            )
        )

    @classmethod
    def _interpretation_from_tool_results(
        cls,
        context: dict[str, Any],
        message: dict[str, Any],
        prepared_calls: list[tuple[dict[str, Any], str, dict[str, Any]]],
        tool_results: dict[str, Any],
    ) -> HotelTurnInterpretation:
        """Create an application-owned interpretation after a read-only lookup."""

        interpretation: HotelTurnInterpretation | None = None
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            try:
                interpretation = cls._parse_response(
                    {"choices": [{"message": {"content": content}}]}
                )
            except (ValueError, ValidationError, KeyError, TypeError, json.JSONDecodeError):
                interpretation = None

        tool_names = tuple(name for _, name, _ in prepared_calls)
        if interpretation is None:
            interpretation = HotelTurnInterpretation(
                intent=("booking" if set(tool_names) & _ROOM_LOOKUP_TOOLS else "general"),
                action="answer",
                dialogue_act="inform",
            )

        updates: dict[str, Any] = {
            "action": "answer",
            "read_only_tools": tool_names,
            "tool_results": tool_results,
            "response_scope": default_response_scope_for_tool(tool_names[0])
            if tool_names
            else interpretation.response_scope,
        }
        search_arguments = next(
            (arguments for _, name, arguments in prepared_calls if name == "search_rooms"),
            None,
        )
        if search_arguments:
            updates.update(
                {
                    "intent": "booking",
                    "booking_check_in": search_arguments.get("check_in"),
                    "booking_check_out": search_arguments.get("check_out"),
                    "booking_guest_count": search_arguments.get("guest_count"),
                    "booking_guest_count_evidence": search_arguments.get(
                        "guest_count_evidence"
                    ),
                    "booking_room_preference": search_arguments.get("room_type")
                    or context.get("booking_room_preference"),
                    "booking_required_features": search_arguments.get(
                        "required_features", []
                    ),
                    "booking_preferred_features": search_arguments.get(
                        "preferred_features", []
                    ),
                    "booking_excluded_features": search_arguments.get(
                        "excluded_features", []
                    ),
                    "booking_removed_features": search_arguments.get(
                        "removed_features", []
                    ),
                    "booking_max_nightly_rate_cents": search_arguments.get(
                        "max_nightly_rate_cents"
                    ),
                    "booking_clear_max_nightly_rate": search_arguments.get(
                        "clear_max_nightly_rate", False
                    ),
                    "booking_bed_type": search_arguments.get("bed_type"),
                    "booking_accessibility_required": search_arguments.get(
                        "accessibility_required"
                    ),
                    "booking_criteria_evidence": search_arguments.get(
                        "criteria_evidence", {}
                    ),
                    "response_scope": search_arguments.get(
                        "response_scope", updates["response_scope"]
                    ),
                }
            )
            check_in = search_arguments.get("check_in")
            check_out = search_arguments.get("check_out")
            try:
                updates["booking_nights"] = (
                    date.fromisoformat(check_out) - date.fromisoformat(check_in)
                ).days
            except (TypeError, ValueError):
                pass
        elif "get_room_details" in tool_names:
            detail_arguments = next(
                (
                    arguments
                    for _, name, arguments in prepared_calls
                    if name == "get_room_details"
                ),
                {},
            )
            updates["booking_room_preference"] = detail_arguments.get("room_type")
            updates["response_scope"] = detail_arguments.get(
                "response_scope", updates["response_scope"]
            )

        return interpretation.model_copy(update=updates)

    @staticmethod
    async def _emit_audit(
        audit_event: HotelAuditEvent | None,
        event: str,
        data: dict[str, Any],
    ) -> None:
        """Record trace evidence without letting diagnostics interrupt a guest call."""

        if not audit_event:
            return
        try:
            await audit_event(event, data)
        except Exception:
            logger.exception("Unable to emit hotel interpreter audit event={}", event)

    @staticmethod
    def _tool_failure(name: str | None) -> HotelTurnInterpretation:
        """Return an application-owned failure marker without invoking a fallback."""

        return HotelTurnInterpretation(
            tool_error=f"read_only_tool_failed:{name or 'unknown'}"
        )

    @staticmethod
    def _missing_guest_count_response(
        response_language: ResponseLanguage,
        arguments: dict[str, Any],
    ) -> HotelTurnInterpretation:
        adapter = language_adapter(
            response_language,
            english_voice="",
            hindi_voice="",
        )

        def valid_date(value: Any) -> str | None:
            if not isinstance(value, str):
                return None
            try:
                return date.fromisoformat(value).isoformat()
            except ValueError:
                return None

        return HotelTurnInterpretation(
            intent="booking",
            action="ask_slot",
            response=adapter.text("booking_guest_count"),
            booking_check_in=valid_date(arguments.get("check_in")),
            booking_check_out=valid_date(arguments.get("check_out")),
        )

    @staticmethod
    def _complete_tool_arguments(
        name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate model-proposed search slots against caller evidence.

        A short follow-up such as ``standard`` may reuse persisted dates and
        guest count. A new count must carry an exact evidence span from the
        current caller transcript.
        """

        if name != "search_rooms":
            return arguments
        completed = dict(arguments)
        supplied_guest_count = completed.get("guest_count")
        if supplied_guest_count is not None:
            supplied_guest_count = _optional_int(
                supplied_guest_count,
                field_name="search_rooms guest_count",
            )
            if supplied_guest_count is None:
                completed.pop("guest_count", None)
            else:
                completed["guest_count"] = supplied_guest_count
        evidence = validate_guest_count_evidence(
            context.get("guest_text"),
            guest_count=supplied_guest_count,
            evidence=completed.get("guest_count_evidence"),
        )
        explicit_guest_count = (
            evidence.guest_count if evidence.status == "explicit" else None
        )
        persisted_guest_count = context.get("booking_guest_count")
        expected_guest_count = (
            explicit_guest_count
            if explicit_guest_count is not None
            else persisted_guest_count
        )
        if expected_guest_count is None:
            raise _MissingCallerFactError(
                "search_rooms requires a caller-confirmed guest_count; ask the caller first"
            )
        if supplied_guest_count is not None:
            if supplied_guest_count != int(expected_guest_count):
                raise ValueError(
                    "search_rooms guest_count does not match caller-confirmed evidence"
                )
        for field in ("check_in", "check_out", "guest_count"):
            if field not in completed and context.get(f"booking_{field}") not in (None, ""):
                completed[field] = context[f"booking_{field}"]
        if "guest_count" not in completed:
            completed["guest_count"] = expected_guest_count
        elif supplied_guest_count is not None:
            completed["guest_count"] = supplied_guest_count
        return completed

    @staticmethod
    def _provider_error_detail(response: httpx.Response) -> str:
        """Extract a compact provider error without logging request credentials."""

        try:
            payload = response.json()
        except json.JSONDecodeError:
            return response.text[:500]
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message[:500]
        return json.dumps(payload)[:500]

    def _payload(self, context: dict[str, Any], *, with_tools: bool) -> dict[str, Any]:
        response_language = context.get("response_language", "en")
        adapter = language_adapter(
            response_language,
            english_voice="",
            hindi_voice="",
        )
        request_context = self._request_context(context, with_tools=with_tools)
        prompt = (
            _TOOL_COMPATIBILITY_SYSTEM_PROMPT
            if with_tools
            else _COMPACT_SYSTEM_PROMPT
        )
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{prompt}\nSelected language contract: "
                        f"{adapter.llm_instruction}"
                    ),
                },
                {"role": "user", "content": json.dumps(request_context)},
            ],
            "max_completion_tokens": (
                300
                if with_tools
                else (96 if request_context.get("expected_booking_slot") else 256)
            ),
        }
        if self._supports_reasoning_effort():
            payload["reasoning_effort"] = self._reasoning_effort
        if with_tools:
            payload["tools"] = HOTEL_TOOL_DEFINITIONS
            payload["tool_choice"] = "auto"
        else:
            payload["response_format"] = (
                {"type": "json_object"}
                if self._uses_groq_json_object_mode()
                else self._structured_response_format()
            )
        return payload

    async def _retry_compact_interpretation(
        self,
        client: httpx.AsyncClient,
        context: dict[str, Any],
        *,
        audit_event: HotelAuditEvent | None,
        initial_error: Exception,
    ) -> HotelTurnInterpretation | None:
        """Retry once with a smaller state-scoped JSON patch after parser failure."""

        retry_context = self._request_context(context, with_tools=False)
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _COMPACT_RETRY_PROMPT},
                {"role": "user", "content": json.dumps(retry_context)},
            ],
            "max_completion_tokens": 128,
            "response_format": {"type": "json_object"},
        }
        if self._supports_reasoning_effort():
            payload["reasoning_effort"] = self._reasoning_effort
        if isinstance(initial_error, httpx.HTTPStatusError):
            reason = self._provider_error_detail(initial_error.response)
        else:
            reason = str(initial_error)
        logger.warning(
            "Hotel semantic parser failed; retrying once with compact context: {}",
            reason,
        )
        await self._emit_audit(
            audit_event,
            "llm_request",
            {"phase": "initial_compact_retry", "payload": payload},
        )
        try:
            response = await self._post_completion(
                client,
                payload,
                audit_event=audit_event,
                audit_phase="initial_compact_retry",
            )
            return self._parse_response(response.json())
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Hotel compact retry was rejected status={} detail={}",
                exc.response.status_code,
                self._provider_error_detail(exc.response),
            )
        except (
            httpx.HTTPError,
            KeyError,
            TypeError,
            ValueError,
            ValidationError,
            json.JSONDecodeError,
        ) as exc:
            logger.warning("Hotel compact parser retry failed: {}", exc)
        return None

    @staticmethod
    def _needs_semantic_slot_repair(
        interpretation: HotelTurnInterpretation,
        context: dict[str, Any],
    ) -> bool:
        """Retry only when the model failed to resolve the active booking slot."""

        if OpenAIHotelTurnInterpreter._expected_booking_slot(context) != (
            "party_composition"
        ):
            return False
        return (
            interpretation.intent in {"unknown", "general", "faq"}
            or interpretation.command == "unknown"
            or interpretation.dialogue_act in {"unknown", "uncertain", "reject"}
            or interpretation.booking_plan_json is None
        )

    @staticmethod
    def _expected_booking_slot(context: dict[str, Any]) -> str | None:
        """Return the one booking fact the current state explicitly needs next."""

        guest_count = context.get("booking_guest_count")
        if not isinstance(guest_count, int) or guest_count <= 1:
            return None
        raw_plan = context.get("booking_plan_json")
        if not isinstance(raw_plan, str) or not raw_plan:
            return "party_composition"
        try:
            plan = json.loads(raw_plan)
        except json.JSONDecodeError:
            return None
        if isinstance(plan, dict) and plan.get("party_composition_known") is not True:
            return "party_composition"
        return None

    @classmethod
    def _request_context(
        cls,
        context: dict[str, Any],
        *,
        with_tools: bool,
    ) -> dict[str, Any]:
        """Keep a focused slot answer independent of unrelated hotel context."""

        if with_tools:
            return context
        expected_slot = cls._expected_booking_slot(context)
        if not expected_slot:
            return context
        fields = (
            "current_date",
            "guest_text",
            "response_language",
            "last_assistant_turn",
            "pending_category",
            "active_topic",
            "last_intent",
            "booking_check_in",
            "booking_check_out",
            "booking_guest_count",
            "booking_nights",
            "booking_pending_fact_confirmation",
            "booking_plan_json",
        )
        compact = {
            field: context[field]
            for field in fields
            if context.get(field) not in (None, "")
            and (
                field != "booking_pending_fact_confirmation" or context[field] is True
            )
        }
        compact["expected_booking_slot"] = expected_slot
        return compact

    def _supports_reasoning_effort(self) -> bool:
        """Avoid provider fields that the selected model family rejects."""

        if "api.groq.com" not in self._base_url.casefold():
            return True
        model = self._model.casefold()
        return model.startswith(("qwen", "openai/gpt-oss"))

    def _uses_groq_json_object_mode(self) -> bool:
        """Use JSON Object Mode unless the Groq model has documented schema support."""

        if "api.groq.com" not in self._base_url.casefold():
            return False
        return not self._model.casefold().startswith("openai/gpt-oss")

    @staticmethod
    def _parse_response(payload: dict[str, Any]) -> HotelTurnInterpretation:
        content = payload["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Hotel interpreter returned empty content")
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
            cleaned = cleaned.rsplit("```", 1)[0].strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end < start:
            raise ValueError("Hotel interpreter did not return JSON")
        parsed = json.loads(cleaned[start : end + 1])
        if not isinstance(parsed, dict):
            raise ValueError("Hotel interpreter response was not an object")
        normalized = dict(parsed)
        # Only the application can claim that it executed a tool or that one
        # failed. These fields are internal execution evidence, not model data.
        normalized.pop("read_only_tools", None)
        normalized.pop("tool_error", None)
        booking_plan = normalized.get("booking_plan_json")
        if isinstance(booking_plan, (dict, list)):
            normalized["booking_plan_json"] = json.dumps(booking_plan, sort_keys=True)
        for key in (
            "name",
            "room_number",
            "service_description",
            "target_request_id",
            "follow_up_question",
            "response",
            "booking_check_in",
            "booking_check_out",
            "booking_guest_count_evidence",
            "booking_room_preference",
            "booking_plan_json",
            "service_category",
            "speech_status",
        ):
            value = normalized.get(key)
            if isinstance(value, int) and key == "room_number":
                normalized[key] = str(value)
            elif isinstance(value, str):
                normalized[key] = value.strip() or None
        normalized["booking_guest_count"] = _optional_int(
            normalized.get("booking_guest_count"),
            field_name="booking_guest_count",
        )
        normalized["booking_nights"] = _optional_int(
            normalized.get("booking_nights"),
            field_name="booking_nights",
        )
        normalized["booking_max_nightly_rate_cents"] = _optional_int(
            normalized.get("booking_max_nightly_rate_cents"),
            field_name="booking_max_nightly_rate_cents",
        )
        normalized["booking_accessibility_required"] = _optional_bool(
            normalized.get("booking_accessibility_required")
        )
        for key in ("explicit_selection", "follow_up_required", "booking_clear_max_nightly_rate"):
            value = normalized.get(key)
            if isinstance(value, str):
                normalized[key] = value.strip().casefold() in {"true", "yes", "y", "1"}
            elif value is None:
                normalized[key] = False
        try:
            normalized["confidence"] = max(
                0.0, min(1.0, float(normalized.get("confidence", 0.0)))
            )
        except (TypeError, ValueError):
            normalized["confidence"] = 0.0
        status_aliases = {
            "final": "complete",
            "completed": "complete",
            "ok": "complete",
            "understood": "complete",
            "interpreted": "complete",
            "recognized": "complete",
            "recognised": "complete",
            "partial": "incomplete",
            "not_clear": "uncertain",
            "unclear": "uncertain",
        }
        status = normalized.get("speech_status")
        if status is None:
            normalized["speech_status"] = "complete"
        elif isinstance(status, str):
            normalized["speech_status"] = status_aliases.get(
                status.casefold(),
                "uncertain",
            )
        else:
            normalized["speech_status"] = "uncertain"
        action_aliases = {
            "continue": "ask_slot",
            "continue_intake": "ask_slot",
            "clarification": "clarify",
            "request_clarification": "clarify",
        }
        action = normalized.get("action")
        if isinstance(action, str):
            normalized["action"] = action_aliases.get(action.casefold(), action)
        aliases = {
            "food": "room_service",
            "drink": "room_service",
            "dining": "room_service",
            "cleaning": "housekeeping",
            "repair": "maintenance",
            "engineering": "maintenance",
        }
        allowed_categories = {
            "room_service",
            "housekeeping",
            "maintenance",
            "security",
            "concierge",
        }
        category = normalized.get("service_category")
        if isinstance(category, str):
            canonical_category = aliases.get(
                re.sub(r"[^a-z0-9]+", "_", category.casefold()).strip("_"),
                re.sub(r"[^a-z0-9]+", "_", category.casefold()).strip("_"),
            )
            normalized["service_category"] = (
                canonical_category if canonical_category in allowed_categories else None
            )
        elif category is not None:
            normalized["service_category"] = None
        intent_aliases = {
            "roomservice": "room_service",
            "room_service_request": "room_service",
            "service_request": normalized.get("service_category", "unknown"),
            "repair": "maintenance",
        }
        intent = normalized.get("intent")
        if isinstance(intent, str):
            normalized["intent"] = intent_aliases.get(intent.casefold(), intent)
        try:
            return HotelTurnInterpretation.model_validate(normalized)
        except ValidationError as exc:
            # Providers occasionally emit an empty string or another invalid
            # value for one optional field despite the structured schema. Do
            # not discard valid booking facts because of that one field.
            invalid_fields = {
                str(error["loc"][0])
                for error in exc.errors()
                if error.get("loc") and isinstance(error["loc"][0], str)
            }
            defaults = HotelTurnInterpretation().model_dump()
            recoverable = invalid_fields & defaults.keys()
            if not recoverable:
                raise
            logger.warning(
                "Hotel interpreter dropped malformed optional fields: {}",
                sorted(recoverable),
            )
            repaired = dict(normalized)
            for field in recoverable:
                repaired[field] = defaults[field]
            return HotelTurnInterpretation.model_validate(repaired)


__all__ = [
    "HotelTurnInterpretation",
    "HotelTurnInterpreter",
    "OpenAIHotelTurnInterpreter",
    "ResponseLanguage",
]
