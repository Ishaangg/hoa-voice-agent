"""Conversation decisions shared by the hotel workflow.

This module is the seam between language interpretation and hotel state.  The
interpreter may suggest meaning, but this module decides whether that meaning
is a caller decision, a question, or merely a recommendation under discussion.
It has no database or PMS dependency, which makes the most failure-prone
conversation transitions replayable without a voice runtime.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from hoa_voice_agent.services.hotel_turn_interpreter import HotelTurnInterpretation

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

ControllerAction = Literal[
    "apply_state",
    "answer_from_state",
    "request_recommendation",
    "select_option",
    "accept_offer",
    "reject_offer",
    "accept_pending_fact",
    "reject_pending_fact",
    "clarify",
    "ignore",
]

ControllerToolName = Literal[
    "list_room_types",
    "search_rooms",
    "get_room_details",
]


@dataclass(frozen=True)
class DialogueSnapshot:
    """The small state projection required by the dialogue controller.

    The controller deliberately receives a snapshot instead of a database
    model.  This keeps the module pure and lets the conversation service own
    persistence while the controller owns transition policy.
    """

    state: str | None = None
    pending_category: str | None = None
    has_active_offer: bool = False
    has_search_evidence: bool = False
    has_booking_dates: bool = False
    has_guest_count: bool = False
    has_room_preference: bool = False
    has_pending_fact_confirmation: bool = False


@dataclass(frozen=True)
class SemanticCommand:
    """Normalized meaning produced by the language interpreter.

    This is intentionally not a database command.  It is an application
    meaning that still requires the controller to validate the current state.
    """

    kind: DialogueCommand
    target: str | None = None
    confidence: float = 0.0
    evidence: dict[str, str] | None = None

    @classmethod
    def from_interpretation(cls, interpretation: Any | None) -> SemanticCommand:
        """Adapt the current interpreter envelope to the new command seam."""

        if interpretation is None:
            return cls("unknown")

        explicit_command = getattr(interpretation, "command", "unknown")
        allowed = {
            "update_facts",
            "ask_about_state",
            "request_recommendation",
            "select_option",
            "accept_offer",
            "reject_offer",
            "cancel",
            "inform",
            "unknown",
        }
        if explicit_command not in allowed:
            explicit_command = "unknown"

        if explicit_command == "unknown":
            # Compatibility adapter for existing interpreter responses.  This
            # keeps old callers working while the model schema migrates to the
            # smaller semantic command contract.
            act = getattr(interpretation, "dialogue_act", "unknown")
            action = getattr(interpretation, "action", "none")
            intent = getattr(interpretation, "intent", "unknown")
            read_only_tools = getattr(interpretation, "read_only_tools", ())
            has_read_only_evidence = bool(read_only_tools)
            if act == "accept" or action == "confirm":
                explicit_command = "accept_offer"
            elif act == "reject":
                explicit_command = "reject_offer"
            elif act == "cancel" or action == "cancel" or intent == "cancellation":
                explicit_command = "cancel"
            elif act == "compare":
                explicit_command = "request_recommendation"
            elif act == "correct":
                explicit_command = "update_facts"
            elif getattr(interpretation, "explicit_selection", False) or act == "choose":
                explicit_command = "select_option"
            elif act == "ask" or action in {"ask_slot", "clarify"}:
                has_booking_candidate = any(
                    getattr(interpretation, field, None) not in (None, "", [])
                    for field in (
                        "booking_check_in",
                        "booking_check_out",
                        "booking_guest_count",
                        "booking_nights",
                        "booking_room_preference",
                        "booking_plan_json",
                    )
                )
                explicit_command = (
                    "update_facts"
                    if intent == "booking" and has_booking_candidate
                    else "ask_about_state"
                )
            elif has_read_only_evidence:
                # A read-only lookup plus a proposed plan is evidence for the
                # conversation, not a caller selection.  This is the
                # structured equivalent of the old text-regex guard and keeps
                # PMS recommendations from becoming booking facts.
                explicit_command = (
                    "request_recommendation"
                    if intent == "booking"
                    else "ask_about_state"
                )
            elif intent == "booking":
                explicit_command = "update_facts"
            else:
                explicit_command = "inform"

        return cls(
            kind=explicit_command,
            target=getattr(interpretation, "command_target", None),
            confidence=float(getattr(interpretation, "confidence", 0.0) or 0.0),
            evidence=getattr(interpretation, "booking_criteria_evidence", None),
        )


@dataclass(frozen=True)
class DialogueDecision:
    """The application-level meaning of one interpreted caller turn."""

    act: DialogueAct
    caller_selected_option: bool
    can_update_booking_plan: bool
    should_refresh_availability: bool
    reason: str


@dataclass(frozen=True)
class ControllerDecision:
    """The controller's state-aware next action."""

    action: ControllerAction
    command: DialogueCommand
    reason: str
    mutates_booking_state: bool = False
    requires_tool: bool = False
    accepts_fact_candidates: bool = False
    tool_name: ControllerToolName | None = None


class HotelDialogueController:
    """Deep, pure policy behind the hotel dialogue seam.

    The model remains responsible for understanding natural language.  The
    controller enforces the distinction that matters for side effects:
    asking about an option is not selecting it, and a recommendation is not
    acceptance. It also combines semantic meaning with the current booking
    snapshot before the conversation service performs a side effect.
    """

    def assess_turn(
        self,
        interpretation: HotelTurnInterpretation | None,
        *,
        has_active_offer: bool = False,
        has_search_evidence: bool = False,
        caller_has_explicit_layout: bool = False,
    ) -> DialogueDecision:
        command = SemanticCommand.from_interpretation(interpretation)
        act = self._dialogue_act(interpretation)
        question_like = command.kind == "ask_about_state" or act == "ask"
        comparison = command.kind == "request_recommendation" or act == "compare"
        caller_layout_signal = bool(
            interpretation
            and (interpretation.explicit_selection or caller_has_explicit_layout)
        )
        if (
            caller_has_explicit_layout
            and interpretation
            and interpretation.dialogue_act == "unknown"
            and command.kind == "request_recommendation"
        ):
            # Older providers sometimes emit a read-only search plus a
            # proposed allocation without filling the new semantic act. The
            # caller-owned layout evidence already reduced by the domain
            # layer disambiguates that turn as a selection.
            act = "choose"
        caller_selected = bool(
            interpretation
            and caller_layout_signal
            and not question_like
            and command.kind
            in {"select_option", "update_facts", "request_recommendation", "inform"}
        )
        if act in {"ask", "compare", "reject", "correct", "backchannel", "uncertain"}:
            caller_selected = False
        can_update = bool(
            interpretation
            and interpretation.intent == "booking"
            and interpretation.booking_plan_json
            and (
                caller_selected
                or (
                    self._contains_party_composition_fact(interpretation)
                    and command.kind in {"update_facts", "inform", "select_option"}
                )
            )
            and (
                command.kind
                in {
                    "select_option",
                    "update_facts",
                    "request_recommendation",
                    "inform",
                }
                or act in {"choose", "accept", "inform", "other"}
            )
        )

        should_refresh = bool(
            interpretation
            and interpretation.intent == "booking"
            and comparison
            and not interpretation.read_only_tools
            and (has_search_evidence or has_active_offer)
        )
        if act == "compare":
            reason = "caller_requested_comparison"
        elif question_like and not caller_selected:
            reason = "question_is_not_selection"
        elif caller_selected:
            reason = "explicit_caller_selection"
        else:
            reason = "no_explicit_selection"
        return DialogueDecision(
            act=act,
            caller_selected_option=caller_selected,
            can_update_booking_plan=can_update,
            should_refresh_availability=should_refresh,
            reason=reason,
        )

    @staticmethod
    def _contains_party_composition_fact(
        interpretation: HotelTurnInterpretation,
    ) -> bool:
        """Recognize structured party facts without classifying caller words."""

        try:
            plan = json.loads(interpretation.booking_plan_json or "{}")
        except json.JSONDecodeError:
            return False
        return isinstance(plan, dict) and (
            "party_groups" in plan or plan.get("party_composition_known") is True
        )

    @staticmethod
    def _dialogue_act(
        interpretation: HotelTurnInterpretation | None,
    ) -> DialogueAct:
        if interpretation and interpretation.dialogue_act != "unknown":
            return interpretation.dialogue_act
        if interpretation:
            command = SemanticCommand.from_interpretation(interpretation)
            if command.kind == "cancel" or interpretation.action == "cancel":
                return "cancel"
            if command.kind == "accept_offer" or interpretation.action == "confirm":
                return "accept"
            if command.kind == "reject_offer":
                return "reject"
            if command.kind == "ask_about_state" or interpretation.action in {
                "ask_slot",
                "clarify",
            }:
                return "ask"
            if interpretation.action == "backchannel":
                return "backchannel"
            if interpretation.intent == "booking":
                if command.kind == "request_recommendation":
                    return "compare"
                if command.kind == "select_option" or interpretation.explicit_selection:
                    return "choose"
                return "inform"
        return "other"


    def decide_turn(
        self,
        snapshot: DialogueSnapshot,
        interpretation: Any | None,
    ) -> ControllerDecision:
        command = SemanticCommand.from_interpretation(interpretation)

        if command.kind == "accept_offer":
            if snapshot.has_pending_fact_confirmation:
                return ControllerDecision(
                    action="accept_pending_fact",
                    command=command.kind,
                    reason="caller_confirmed_pending_fact",
                    mutates_booking_state=True,
                )
            if snapshot.has_active_offer and snapshot.state == "awaiting_booking_confirmation":
                return ControllerDecision(
                    action="accept_offer",
                    command=command.kind,
                    reason="caller_confirmed_active_offer",
                )
            return ControllerDecision(
                action="clarify",
                command=command.kind,
                reason="acceptance_without_pending_confirmation",
            )

        if command.kind == "reject_offer":
            if snapshot.has_pending_fact_confirmation:
                return ControllerDecision(
                    action="reject_pending_fact",
                    command=command.kind,
                    reason="caller_rejected_pending_fact",
                    mutates_booking_state=True,
                )
            if snapshot.has_active_offer and snapshot.state == "awaiting_booking_confirmation":
                return ControllerDecision(
                    action="reject_offer",
                    command=command.kind,
                    reason="caller_rejected_active_offer",
                )
            return ControllerDecision(
                action="clarify",
                command=command.kind,
                reason="rejection_without_pending_confirmation",
            )

        if command.kind == "ask_about_state":
            if command.target in {"catalogue", "room_catalogue", "room_types"}:
                return ControllerDecision(
                    action="answer_from_state",
                    command=command.kind,
                    reason="caller_requested_verified_room_catalogue",
                    requires_tool=True,
                    tool_name="list_room_types",
                )
            if command.target in {
                "room_details",
                "room",
                "amenities",
                "capacity",
                "price",
                "features",
            } and snapshot.has_room_preference:
                return ControllerDecision(
                    action="answer_from_state",
                    command=command.kind,
                    reason="caller_requested_verified_room_details",
                    requires_tool=True,
                    tool_name="get_room_details",
                )
            if snapshot.has_active_offer or snapshot.pending_category == "booking":
                return ControllerDecision(
                    action="answer_from_state",
                    command=command.kind,
                    reason="question_is_about_current_conversation_state",
                )
            return ControllerDecision(
                action="clarify",
                command=command.kind,
                reason="no_current_state_for_question",
            )

        if command.kind == "request_recommendation":
            if snapshot.has_booking_dates and snapshot.has_guest_count:
                return ControllerDecision(
                    action="request_recommendation",
                    command=command.kind,
                    reason="required_availability_facts_are_known",
                    requires_tool=True,
                    accepts_fact_candidates=True,
                )
            return ControllerDecision(
                action="clarify",
                command=command.kind,
                reason="recommendation_requires_dates_and_guest_count",
            )

        if command.kind == "select_option":
            return ControllerDecision(
                action="select_option",
                command=command.kind,
                reason="caller_explicitly_selected_an_option",
                mutates_booking_state=True,
                accepts_fact_candidates=True,
            )

        if command.kind == "update_facts":
            return ControllerDecision(
                action="apply_state",
                command=command.kind,
                reason="caller_provided_booking_information",
                mutates_booking_state=True,
                accepts_fact_candidates=True,
            )

        if command.kind == "cancel":
            return ControllerDecision(
                action="apply_state",
                command=command.kind,
                reason="caller_requested_cancellation",
                mutates_booking_state=True,
            )

        if command.kind == "inform":
            return ControllerDecision(
                action="apply_state",
                command=command.kind,
                reason="caller_provided_information",
                mutates_booking_state=True,
                accepts_fact_candidates=True,
            )

        return ControllerDecision(
            action="clarify",
            command=command.kind,
            reason="semantic_command_is_unknown",
        )

def select_turn_response(
    *,
    interpretation: HotelTurnInterpretation | None,
    verified_response: str | None,
    verified_evidence_is_current: bool,
) -> str | None:
    """Compose from the current turn, never from stale saved speech.

    PMS evidence may replace model prose only when that evidence was produced
    during this turn.  A previous search is context for the interpreter, not a
    response that can override the caller's newest question.
    """

    model_response = interpretation.response if interpretation else None
    if verified_evidence_is_current and verified_response:
        return verified_response
    return model_response


__all__ = [
    "ControllerDecision",
    "ControllerAction",
    "DialogueAct",
    "DialogueCommand",
    "DialogueDecision",
    "DialogueSnapshot",
    "HotelDialogueController",
    "SemanticCommand",
    "select_turn_response",
]
