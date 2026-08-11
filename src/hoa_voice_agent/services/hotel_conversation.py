"""Hotel guest-service conversation orchestration.

The HOA service is deliberately left untouched. This service owns hotel
language, request categories, guest context, and the fake PMS boundary while
sharing only infrastructure-level adapters (database session and messaging).
"""

# ruff: noqa: E501

import json
import re
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.adapters.hotel_pms import HotelPMSAdapter
from hoa_voice_agent.adapters.protocols import MessagingAdapter
from hoa_voice_agent.persistence.hotel_repository import HotelStore
from hoa_voice_agent.persistence.hotel_tables import HotelRequestRecord
from hoa_voice_agent.services.hotel_audit import HotelConversationAuditLog
from hoa_voice_agent.services.hotel_booking_coordinator import HotelBookingCoordinator
from hoa_voice_agent.services.hotel_booking_facts import (
    BookingFactLedger,
    FactProposal,
)
from hoa_voice_agent.services.hotel_booking_flow import (
    BookingSlotUpdate,
    BookingStateSnapshot,
    HotelBookingFlow,
    explicit_guest_count_evidence_from_text,
    validate_guest_count_evidence,
)
from hoa_voice_agent.services.hotel_booking_offers import HotelBookingOfferService
from hoa_voice_agent.services.hotel_booking_plan import (
    BookingPlan,
)
from hoa_voice_agent.services.hotel_catalog import (
    CatalogSnapshotService,
    HotelCatalogSnapshot,
)
from hoa_voice_agent.services.hotel_dialogue_kernel import (
    ControllerDecision,
    DialogueSnapshot,
    HotelDialogueController,
    select_turn_response,
)
from hoa_voice_agent.services.hotel_recommendations import BookingCriteria
from hoa_voice_agent.services.hotel_tools import default_response_scope_for_tool
from hoa_voice_agent.services.hotel_turn_interpreter import (
    HotelTurnInterpretation,
    HotelTurnInterpreter,
    ResponseLanguage,
)
from hoa_voice_agent.services.outbox import OutboxNotificationQueue
from hoa_voice_agent.voice.language import (
    LanguageAdapter,
)
from hoa_voice_agent.voice.language import (
    language_adapter as build_language_adapter,
)


@dataclass(frozen=True)
class HotelTurnResponse:
    conversation_id: UUID
    intent: str
    say: str
    request_id: UUID | None = None


@dataclass(frozen=True)
class HotelVoiceRoute:
    system_prompt: str | None = None
    response: HotelTurnResponse | None = None


class HotelConversationNotFoundError(Exception):
    pass


class HotelConversationService:
    _SUCCESS_ACKNOWLEDGEMENTS = (
        "Got it -",
        "All set -",
        "Thanks, I've got that -",
    )

    _request_rules: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
        (
            "engineering",
            "urgent",
            "maintenance",
            (
                "leak",
                "leaking",
                "maintenance",
                "engineering",
                "no hot water",
                "air conditioning",
                "ac ",
                "heater",
                "toilet",
                "outlet",
                "power",
            ),
        ),
        (
            "housekeeping",
            "routine",
            "housekeeping",
            (
                "towel",
                "towels",
                "clean my room",
                "housekeeping",
                "toiletries",
                "soap",
                "water bottle",
            ),
        ),
        (
            "concierge",
            "routine",
            "concierge",
            ("restaurant", "taxi", "recommend", "reservation", "directions", "tour"),
        ),
        (
            "food_beverage",
            "routine",
            "room_service",
            (
                "room service",
                "in my room",
                "to my room",
                "noodles",
                "maggie",
                "maggi",
                "manchurian",
                "momos",
                "menu",
                "order food",
                "food order",
                "meal",
                "hungry",
            ),
        ),
        (
            "security",
            "emergency",
            "security",
            ("unsafe", "threat", "fire", "smoke", "medical", "locked out"),
        ),
    )

    def __init__(
        self,
        session: AsyncSession,
        hotel_id: UUID,
        messaging: MessagingAdapter | None = None,
        notification_channel: str = "whatsapp",
        notification_queue: OutboxNotificationQueue | None = None,
        interpreter: HotelTurnInterpreter | None = None,
        allow_legacy_language_rules: bool = False,
        audit_log: HotelConversationAuditLog | None = None,
        response_language: ResponseLanguage = "en",
        language_adapter: LanguageAdapter | None = None,
        telemetry: Any | None = None,
    ) -> None:
        self.session = session
        self.hotel_id = hotel_id
        self.store = HotelStore(session)
        self.messaging = messaging
        self.notification_channel = notification_channel
        self.notification_queue = notification_queue
        self.interpreter = interpreter
        # The new semantic interpreter/controller path keeps tool choice and
        # execution in this service.  Test and legacy adapters that explicitly
        # expose the callback continue to receive it during migration.
        self._controller_owns_tools = bool(
            getattr(interpreter, "controller_owns_tools", False)
        )
        self.allow_legacy_language_rules = allow_legacy_language_rules
        self.audit_log = audit_log
        self.language_adapter = language_adapter or build_language_adapter(
            response_language,
            english_voice="",
            hindi_voice="",
        )
        self.response_language = self.language_adapter.mode
        self.telemetry = telemetry
        self.booking_flow = HotelBookingFlow()
        self.booking_coordinator = HotelBookingCoordinator(
            session,
            hotel_id,
            booking_flow=self.booking_flow,
            confirmation_notifier=self._send_booking_confirmation_whatsapp,
            language_adapter=self.language_adapter,
            telemetry=telemetry,
        )
        # One pure decision module owns caller intent, recommendations, and
        # accepted booking choices. Persistence and PMS effects stay here.
        self.dialogue_controller = HotelDialogueController()
        self._success_ack_index = 0
        self._booking_lookup_completed_this_turn = False
        self._party_composition_required_context: tuple[date, date, int] | None = None

    def _success_acknowledgement(self, message: str) -> str:
        """Add a varied, concise acknowledgement after a committed action."""

        prefix = self._SUCCESS_ACKNOWLEDGEMENTS[
            self._success_ack_index % len(self._SUCCESS_ACKNOWLEDGEMENTS)
        ]
        self._success_ack_index += 1
        return f"{prefix} {message}"

    @staticmethod
    def _booking_response(conversation, result) -> HotelTurnResponse:
        """Adapt the booking module's response to the conversation API."""

        return HotelTurnResponse(
            conversation.id,
            result.intent,
            result.say,
            result.request_id,
        )

    @staticmethod
    def _format_booking_date(value: date) -> str:
        """Format a booking date without platform-specific strftime flags."""

        return f"{value.strftime('%B')} {value.day}, {value.year}"

    @staticmethod
    def _booking_room_summary(rooms: list[dict[str, Any]]) -> str:
        """Summarize confirmed room lines in natural language."""

        counts: dict[str, int] = {}
        for room in rooms:
            room_type = str(room.get("room_type") or "room")
            counts[room_type] = counts.get(room_type, 0) + 1
        parts = []
        for room_type, count in counts.items():
            label = room_type.title()
            parts.append(f"{count} {label} room{'s' if count != 1 else ''}")
        return " and ".join(parts) or "the selected rooms"

    async def _send_booking_confirmation_whatsapp(
        self,
        *,
        conversation,
        confirmed: dict[str, Any],
        room_summary: str,
    ) -> bool:
        """Queue the confirmed stay details for the caller when WhatsApp is active."""

        if self.notification_channel != "whatsapp":
            return False
        if not self.messaging and not self.notification_queue:
            return False
        check_in = conversation.booking_check_in
        check_out = conversation.booking_check_out
        if not check_in or not check_out:
            return False
        message = (
            "Evergreen Harbor Hotel booking confirmed. "
            f"Confirmation: {confirmed['confirmation_code']}. "
            f"Stay: {self._format_booking_date(check_in)} through "
            f"{self._format_booking_date(check_out)}. "
            f"Rooms: {room_summary}. "
            f"Guests: {conversation.booking_guest_count or 'as requested'}."
        )
        try:
            await self._notify(
                conversation.caller_phone,
                message,
                idempotency_key=f"hotel-booking:{confirmed['reservation_id']}",
                aggregate_type="hotel_reservation",
                aggregate_id=UUID(confirmed["reservation_id"]),
            )
        except Exception as exc:  # the confirmed reservation must remain intact
            logger.warning(
                "Hotel booking WhatsApp delivery failed reservation_id={}: {}",
                confirmed["reservation_id"],
                exc,
            )
            return False
        return True

    async def _record_audit(
        self,
        conversation_id: UUID,
        event: str,
        data: dict[str, Any],
    ) -> None:
        if self.audit_log:
            await self.audit_log.record(conversation_id, event, data)

    async def _active_booking_room_type(self, conversation, text: str) -> str | None:
        """Resolve a concise caller selection against the active room catalogue."""

        if (
            conversation.pending_category != "booking"
            or not conversation.booking_check_in
            or not conversation.booking_check_out
            or not conversation.booking_guest_count
        ):
            return None
        candidate = " ".join(text.casefold().strip(" .?!").split())
        candidate = re.sub(
            r"^(?:we can have|can we have|we(?: would|'d) like|i(?: would|'d) like|"
            r"we want|i want|let'?s have|make it|consider)\s+",
            "",
            candidate,
        )
        candidate = re.sub(r"^(?:a|an|the)\s+", "", candidate)
        candidate = re.sub(r"\s+(?:room\s+type|rooms?|type)$", "", candidate)
        if not candidate:
            return None
        room_types = await HotelPMSAdapter(self.session, self.hotel_id).room_types()
        return next(
            (room_type for room_type in room_types if room_type.casefold() == candidate),
            None,
        )

    async def _handle_active_booking_room_type(
        self,
        conversation,
        text: str,
        *,
        on_response: Callable[[str], Awaitable[None]] | None = None,
    ) -> HotelTurnResponse | None:
        """Search a selected type using persisted dates and guest count.

        This is still a PMS lookup. The application supplies the booking facts
        it already owns instead of making the LLM recreate tool arguments from
        a one- or two-word follow-up.
        """

        room_type = await self._active_booking_room_type(conversation, text)
        if not room_type:
            return None
        if on_response:
            await on_response("Let me check the available room options for you.")
        plan = self.booking_flow.availability_plan(
            check_in=conversation.booking_check_in,
            check_out=conversation.booking_check_out,
            guest_count=conversation.booking_guest_count,
            room_type=room_type,
        )
        if not plan:
            return None
        arguments = plan.arguments
        result = await HotelPMSAdapter(
            self.session, self.hotel_id
        ).search_booking_availability(
            check_in=conversation.booking_check_in,
            check_out=conversation.booking_check_out,
            guest_count=conversation.booking_guest_count,
            requested_room_type=room_type,
            booking_plan=BookingPlan.from_json(conversation.booking_plan_json),
        )
        await self._remember_hotel_tool_result(
            plan.name,
            arguments,
            result,
            conversation.id,
        )
        await self._record_audit(
            conversation.id,
            "pms_tool_result",
            {
                "source": "active_booking_room_type",
                "tool": plan.name,
                "arguments": arguments,
                "result": result,
            },
        )
        # This is the caller explicitly selecting a type, unlike a model
        # merely mentioning an alternative in a search result.
        conversation.booking_room_preference = room_type
        say = self.booking_coordinator.verified_room_search_response(conversation)
        if not say:
            return None
        self._remember_turn_state(conversation, intent="booking", topic="booking")
        await self.store.add_turn(conversation.id, "assistant", say)
        await self.session.commit()
        return HotelTurnResponse(conversation.id, "booking", say)

    async def _interpret_turn(
        self,
        conversation,
        text: str,
        *,
        turn_id: str | None = None,
        on_response: Callable[[str], Awaitable[None]] | None = None,
    ) -> HotelTurnInterpretation | None:
        if not self.interpreter:
            return None
        history = await self.store.request_history(conversation.id)
        request_history = [
            {
                "category": row.category,
                "description": row.description,
                "status": row.status,
                "room_number": row.room_number,
            }
            for row in history
        ]

        async def audit_event(event: str, data: dict[str, Any]) -> None:
            await self._record_audit(conversation.id, event, data)

        interpret_kwargs: dict[str, Any] = {
            "text": text,
            "last_assistant_turn": await self.store.latest_assistant_turn(conversation.id),
            "caller_name": conversation.caller_name,
            "room_number": conversation.room_number,
            "pending_category": conversation.pending_category,
            "pending_description": conversation.pending_description,
            "active_topic": conversation.active_topic,
            "last_intent": conversation.last_intent,
            "response_language": self.response_language,
            "approved_facts": await self.store.active_facts(self.hotel_id),
            "request_history": request_history,
            "on_response": on_response,
            "audit_event": audit_event,
            "booking_check_in": (
                conversation.booking_check_in.isoformat()
                if conversation.booking_check_in
                else None
            ),
            "booking_check_out": (
                conversation.booking_check_out.isoformat()
                if conversation.booking_check_out
                else None
            ),
            "booking_guest_count": conversation.booking_guest_count,
            "booking_nights": conversation.booking_nights,
            "booking_room_preference": conversation.booking_room_preference,
            "booking_facts_json": conversation.booking_facts_json,
            "booking_pending_fact_confirmation": self._booking_fact_ledger(
                conversation
            ).has_pending(),
            "booking_plan_json": conversation.booking_plan_json,
            "booking_last_search_result": conversation.booking_last_search_result,
            "booking_criteria_json": conversation.booking_criteria_json,
            "booking_catalog_snapshot_version": conversation.booking_catalog_snapshot_version,
            "booking_catalog_relevant_json": conversation.booking_catalog_relevant_json,
        }
        if not self._controller_owns_tools:
            interpret_kwargs["tool_executor"] = lambda name, arguments: self._execute_hotel_tool(
                conversation.id,
                name,
                arguments,
                caller_text=text,
                caller_turn_id=turn_id,
            )
        return await self.interpreter.interpret(
            **interpret_kwargs,
        )

    async def _execute_hotel_tool(
        self,
        conversation_id: UUID,
        name: str,
        arguments: dict[str, Any],
        *,
        caller_text: str | None = None,
        caller_turn_id: str | None = None,
    ) -> dict[str, Any]:
        """Execute only the read-only PMS tools exposed to the model."""

        pms = HotelPMSAdapter(self.session, self.hotel_id)
        result: dict[str, Any]
        if name == "list_room_types":
            catalog = await CatalogSnapshotService(
                self.session, self.hotel_id, telemetry=self.telemetry
            ).get()
            result = {
                "room_types": list(catalog.room_type_keys),
                "catalog_version": catalog.version,
            }
            await self._remember_hotel_tool_result(
                name, arguments, result, conversation_id, caller_text=caller_text
            )
            await self._record_audit(
                conversation_id,
                "pms_tool_result",
                {"tool": name, "arguments": arguments, "result": result},
            )
            return result
        if name == "search_rooms":
            conversation = await self.store.conversation(conversation_id)
            supplied_guest_count = arguments.get("guest_count")
            try:
                supplied_guest_count = int(supplied_guest_count)
            except (TypeError, ValueError) as exc:
                raise ValueError("search_rooms guest_count is missing or invalid") from exc
            evidence = validate_guest_count_evidence(
                caller_text,
                guest_count=supplied_guest_count,
                evidence=arguments.get("guest_count_evidence"),
            )
            ledger = self._booking_fact_ledger(conversation) if conversation else None
            persisted_guest_count = ledger.active_value("guest_count") if ledger else None
            expected_guest_count = (
                evidence.guest_count
                if evidence.status in {"explicit", "semantic"}
                else persisted_guest_count
            )
            if expected_guest_count is None:
                raise ValueError(
                    "search_rooms requires a caller-confirmed guest count; ask the caller first"
                )
            actual_guest_count = supplied_guest_count
            check_in = date.fromisoformat(arguments["check_in"])
            check_out = date.fromisoformat(arguments["check_out"])
            staged_proposals: list[FactProposal] = []
            if conversation:
                _, staged_proposals = self._stage_booking_fact_changes(
                    conversation,
                    BookingSlotUpdate(
                        check_in=check_in,
                        check_out=check_out,
                        guest_count=actual_guest_count,
                        guest_count_evidence=arguments.get("guest_count_evidence"),
                        guest_count_evidence_status=evidence.status,
                    ),
                    caller_text=caller_text or "",
                    source_turn_id=caller_turn_id or "tool",
                )
                requested_room_type = arguments.get("room_type")
                if isinstance(requested_room_type, str) and requested_room_type:
                    room_ledger = self._booking_fact_ledger(conversation)
                    room_proposal = room_ledger.propose(
                        "room_preference",
                        requested_room_type,
                        evidence=caller_text,
                        source_turn_id=caller_turn_id or "tool",
                        evidence_status=(
                            "explicit"
                            if requested_room_type.casefold() in (caller_text or "").casefold()
                            else "semantic"
                        ),
                    )
                    staged_proposals.append(room_proposal)
                    self._sync_booking_fact_projection(conversation, room_ledger)
            if any(
                proposal.outcome in {"pending_confirmation", "ambiguous"}
                for proposal in staged_proposals
            ):
                return {
                    "status": "confirmation_required",
                    "pending_facts": [
                        {
                            "field": proposal.field,
                            "current_value": proposal.current_value,
                            "proposed_value": proposal.proposed_value,
                        }
                        for proposal in staged_proposals
                        if proposal.outcome in {"pending_confirmation", "ambiguous"}
                    ],
                }
            if actual_guest_count != int(expected_guest_count):
                raise ValueError(
                    "search_rooms guest_count does not match caller-confirmed evidence"
                )
            booking_plan = BookingPlan.from_json(
                conversation.booking_plan_json if conversation else None
            )
            if (
                conversation
                and actual_guest_count > 1
                and not booking_plan.party_composition_known
            ):
                self._party_composition_required_context = (
                    check_in,
                    check_out,
                    actual_guest_count,
                )
                return {
                    "status": "party_composition_required",
                    "guest_count": actual_guest_count,
                    "question": self.language_adapter.text(
                        "booking_party_composition",
                        guest_count=actual_guest_count,
                    ),
                }
            catalog = await CatalogSnapshotService(
                self.session, self.hotel_id, telemetry=self.telemetry
            ).get()
            if conversation:
                criteria_changed = await self._apply_booking_criteria_patch(
                    conversation,
                    arguments,
                    caller_text=caller_text or "",
                    catalog=catalog,
                    source_turn_id=caller_turn_id,
                )
                if criteria_changed:
                    await self._record_audit(
                        conversation_id,
                        "booking_criteria_updated",
                        {"criteria": conversation.booking_criteria_json},
                    )
            criteria = BookingCriteria.from_json(
                conversation.booking_criteria_json if conversation else None
            )
            if conversation:
                # The fact ledger has already projected the validated active
                # values. Keep the conversation in booking context while the
                # PMS lookup runs; a correction cannot reach this branch until
                # the caller confirms it.
                conversation.pending_category = "booking"
                conversation.active_topic = "booking"
            result = await pms.search_booking_availability(
                check_in=check_in,
                check_out=check_out,
                guest_count=actual_guest_count,
                requested_room_type=arguments.get("room_type"),
                catalog=catalog,
                criteria=criteria if not criteria.is_empty() else None,
                booking_plan=booking_plan,
                telemetry=self.telemetry,
            )
            result["catalog_version"] = catalog.version
            await self._remember_hotel_tool_result(
                name, arguments, result, conversation_id, caller_text=caller_text
            )
            await self._record_audit(
                conversation_id,
                "pms_tool_result",
                {"tool": name, "arguments": arguments, "result": result},
            )
            return result
        if name == "get_room_details":
            catalog = await CatalogSnapshotService(
                self.session, self.hotel_id, telemetry=self.telemetry
            ).get()
            result = {
                "rooms": await pms.get_room_details(
                    room_id=arguments.get("room_id"),
                    room_type=arguments.get("room_type"),
                )
            }
            result["catalog_version"] = catalog.version
            await self._remember_hotel_tool_result(
                name, arguments, result, conversation_id, caller_text=caller_text
            )
            await self._record_audit(
                conversation_id,
                "pms_tool_result",
                {"tool": name, "arguments": arguments, "result": result},
            )
            return result
        if name == "get_open_requests":
            # The conversation id is injected by the service; the model is
            # never allowed to choose which guest's requests it can inspect.
            rows = await self.store.request_history(conversation_id)
            status = arguments.get("status", "open")
            selected = rows if status == "all" else [row for row in rows if row.status == status]
            room_number = arguments.get("room_number")
            if room_number:
                selected = [row for row in selected if row.room_number == room_number]
            selected = selected[: arguments.get("limit", 10)]
            result = {
                "requests": [
                    {
                        "request_id": str(row.id),
                        "category": row.category,
                        "description": row.description,
                        "status": row.status,
                        "priority": row.priority,
                        "room_number": row.room_number,
                    }
                    for row in selected
                ]
            }
            await self._record_audit(
                conversation_id,
                "pms_tool_result",
                {"tool": name, "arguments": arguments, "result": result},
            )
            return result
        raise ValueError(f"Unsupported hotel tool: {name}")

    async def _apply_booking_criteria_patch(
        self,
        conversation,
        raw_patch: dict[str, Any],
        *,
        caller_text: str,
        catalog: HotelCatalogSnapshot,
        source_turn_id: str | None = None,
    ) -> bool:
        """Persist only catalogue-backed caller requirements."""

        verified_evidence = {
            str(key): value.strip()
            for key, value in (raw_patch.get("criteria_evidence") or {}).items()
            if isinstance(key, str)
            and isinstance(value, str)
            and value.strip()
            and value.casefold() in caller_text.casefold()
        }
        amenity_terms = {
            amenity.key: {amenity.key.casefold(), amenity.name.casefold()}
            for entry in catalog.room_types
            for amenity in entry.amenities
        }

        def canonical_features(value: Any, evidence_key: str) -> list[str]:
            if not isinstance(value, (list, tuple)):
                return []
            evidence_text = verified_evidence.get(evidence_key, "").casefold()
            if not evidence_text:
                return []
            output: list[str] = []
            for item in value:
                if not isinstance(item, str):
                    continue
                canonical = catalog.resolve_feature_key(item)
                if (
                    canonical
                    and canonical not in output
                    and any(term in evidence_text for term in amenity_terms[canonical])
                ):
                    output.append(canonical)
            return output

        raw_budget = raw_patch.get("max_nightly_rate_cents")
        try:
            budget = int(raw_budget) if raw_budget is not None else None
        except (TypeError, ValueError):
            budget = None
        if budget is not None and not any(
            key in verified_evidence
            for key in ("max_nightly_rate_cents", "budget", "nightly_budget")
        ):
            budget = None
        bed_type = raw_patch.get("bed_type")
        known_beds = {
            entry.bed_type.casefold()
            for entry in catalog.room_types
            if entry.bed_type
        }
        bed_evidence = verified_evidence.get("bed_type", "").casefold()
        if (
            not isinstance(bed_type, str)
            or bed_type.casefold() not in known_beds
            or bed_type.casefold() not in bed_evidence
        ):
            bed_type = None
        accessibility_required = raw_patch.get("accessibility_required")
        if not isinstance(accessibility_required, bool) or not any(
            key in verified_evidence
            for key in ("accessibility_required", "accessibility")
        ):
            accessibility_required = None
        clear_budget = bool(raw_patch.get("clear_max_nightly_rate", False)) and any(
            key in verified_evidence
            for key in ("clear_max_nightly_rate", "budget", "nightly_budget")
        )

        current = BookingCriteria.from_json(conversation.booking_criteria_json)
        updated = current.apply_patch(
            required_features=canonical_features(
                raw_patch.get("required_features"), "required_features"
            ),
            preferred_features=canonical_features(
                raw_patch.get("preferred_features"), "preferred_features"
            ),
            excluded_features=canonical_features(
                raw_patch.get("excluded_features"), "excluded_features"
            ),
            removed_features=canonical_features(
                raw_patch.get("removed_features"), "removed_features"
            ),
            max_nightly_rate_cents=budget,
            clear_max_nightly_rate=clear_budget,
            bed_type=bed_type,
            accessibility_required=accessibility_required,
            evidence=verified_evidence,
            known_features=catalog.feature_keys,
        )
        if updated == current:
            return False
        ledger = self._booking_fact_ledger(conversation)
        proposal = ledger.propose(
            "criteria",
            updated.to_json(),
            evidence=next(iter(verified_evidence.values()), caller_text),
            source_turn_id=source_turn_id,
            evidence_status="explicit" if verified_evidence else "semantic",
        )
        self._sync_booking_fact_projection(conversation, ledger)
        if proposal.outcome in {"pending_confirmation", "ambiguous"}:
            return False
        if conversation.booking_hold_reservation_id:
            await HotelPMSAdapter(self.session, self.hotel_id).cancel_booking_hold(
                conversation.booking_hold_reservation_id
            )
            conversation.booking_hold_reservation_id = None
        await HotelBookingOfferService(
            self.session, self.hotel_id, telemetry=self.telemetry
        ).invalidate_presented_offers(
            conversation,
            reason="The caller changed the room requirements.",
            commit=False,
        )
        conversation.booking_criteria_json = updated.to_json()
        conversation.booking_criteria_revision = updated.revision
        conversation.booking_catalog_snapshot_version = catalog.version
        conversation.booking_catalog_relevant_json = json.dumps(
            {"version": catalog.version, "room_types": catalog.relevant()}, sort_keys=True
        )
        return True

    async def _remember_hotel_tool_result(
        self,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        conversation_id: UUID,
        caller_text: str | None = None,
    ) -> None:
        """Persist the latest booking lookup for the next interpreted turn."""

        conversation = await self.store.conversation(conversation_id)
        if conversation and name in {"list_room_types", "search_rooms", "get_room_details"}:
            self._booking_lookup_completed_this_turn = True
            await HotelBookingOfferService(
                self.session, self.hotel_id, telemetry=self.telemetry
            ).record_tool_event(
                conversation_id=conversation_id,
                tool_name=name,
                arguments=arguments,
                result=result,
            )
            requested_scope = arguments.get("response_scope")
            scope = (
                requested_scope
                if isinstance(requested_scope, str) and requested_scope
                else default_response_scope_for_tool(name)
            )
            conversation.booking_response_scope = scope
            if self.telemetry:
                self.telemetry.record(
                    "response_scope",
                    metadata={"scope": scope, "tool": name},
                )
            conversation.booking_last_search_result = json.dumps(
                {"tool": name, "result": result, "response_scope": scope},
                sort_keys=True,
            )
            if isinstance(result.get("catalog_version"), int):
                conversation.booking_catalog_snapshot_version = result["catalog_version"]
            relevant_types: set[str] = set()
            for item in result.get("available_room_types", []):
                if isinstance(item, dict) and isinstance(item.get("room_type"), str):
                    relevant_types.add(item["room_type"])
            for item in result.get("recommendations", []):
                if isinstance(item, dict):
                    allocation = item.get("allocation")
                    if isinstance(allocation, list):
                        for line in allocation:
                            if isinstance(line, dict) and isinstance(line.get("room_type"), str):
                                relevant_types.add(line["room_type"])
            for room in result.get("rooms", []):
                if isinstance(room, dict) and isinstance(room.get("room_type"), str):
                    relevant_types.add(room["room_type"])
            if isinstance(result.get("room_types"), list):
                relevant_types.update(
                    value for value in result["room_types"] if isinstance(value, str)
                )
            if name == "get_room_details":
                relevant_types.update(
                    room.get("room_type")
                    for room in result.get("rooms", [])
                    if isinstance(room, dict) and isinstance(room.get("room_type"), str)
                )
            catalog = await CatalogSnapshotService(
                self.session, self.hotel_id, telemetry=self.telemetry
            ).get()
            conversation.booking_catalog_relevant_json = json.dumps(
                {
                    "version": catalog.version,
                    "room_types": catalog.relevant(relevant_types or None),
                },
                sort_keys=True,
            )

    async def _refresh_booking_options_for_comparison(
        self,
        conversation,
        *,
        on_response: Callable[[str], Awaitable[None]] | None = None,
    ) -> HotelTurnResponse | None:
        """Run the bounded availability query required by an alternatives turn.

        "Other options" is a new information request, not permission to reuse
        the previous answer.  The kernel decides when this operation is
        required; this method owns one fresh, unfiltered PMS lookup and turns
        its evidence into a comparison response.
        """

        plan = self.booking_flow.availability_plan(
            check_in=conversation.booking_check_in,
            check_out=conversation.booking_check_out,
            guest_count=conversation.booking_guest_count,
            purpose="comparison",
        )
        if not plan:
            return None
        if on_response:
            await on_response("Let me check the other room options for those dates.")
        await self._execute_hotel_tool(
            conversation.id,
            plan.name,
            plan.arguments,
        )
        say = await self.booking_coordinator.recommended_offer_response(conversation)
        if not say:
            say = self.booking_coordinator.verified_room_search_response(
                conversation,
                comparison=True,
            )
        if not say:
            return None
        conversation.pending_category = "booking"
        self._remember_turn_state(conversation, intent="booking", topic="booking")
        await self.store.add_turn(conversation.id, "assistant", say)
        await self.session.commit()
        return HotelTurnResponse(conversation.id, "booking_options", say)

    async def _run_controller_recommendation(
        self,
        conversation,
        *,
        on_response: Callable[[str], Awaitable[None]] | None = None,
    ) -> HotelTurnResponse | None:
        """Execute the controller's one bounded recommendation lookup.

        The semantic interpreter identifies that the caller wants a
        recommendation.  The controller waits until the reducer has applied
        the current booking facts, then this service performs exactly one
        dated PMS search and renders its verified result.  No model summary
        round trip and no per-room lookup are needed.
        """

        plan = self.booking_flow.availability_plan(
            check_in=conversation.booking_check_in,
            check_out=conversation.booking_check_out,
            guest_count=conversation.booking_guest_count,
            purpose="availability",
        )
        if not plan:
            return None
        if on_response:
            await on_response(
                self.language_adapter.tool_progress("search_rooms", phrase_index=0)
            )
        result = await self._execute_hotel_tool(
            conversation.id,
            plan.name,
            {**plan.arguments, "response_scope": "recommendation"},
        )
        if result.get("status") == "party_composition_required":
            say = result.get("question")
            if not isinstance(say, str) or not say:
                say = self.language_adapter.text(
                    "booking_party_composition",
                    guest_count=conversation.booking_guest_count,
                )
            conversation.pending_category = "booking"
            conversation.active_topic = "booking"
            conversation.state = "collecting_booking_allocation"
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "booking", say)

        say = await self.booking_coordinator.recommended_offer_response(conversation)
        if not say:
            say = self.booking_coordinator.verified_room_search_response(conversation)
        if not say:
            return None
        conversation.pending_category = "booking"
        self._remember_turn_state(conversation, intent="booking", topic="booking")
        await self.store.add_turn(conversation.id, "assistant", say)
        await self.session.commit()
        return HotelTurnResponse(conversation.id, "booking", say)

    async def _run_controller_state_lookup(
        self,
        conversation,
        decision: ControllerDecision,
    ) -> HotelTurnResponse | None:
        """Answer a semantic state question from one verified read result."""

        tool_name = decision.tool_name
        if tool_name == "list_room_types":
            arguments = {"response_scope": "catalogue"}
        elif tool_name == "get_room_details" and conversation.booking_room_preference:
            arguments = {
                "room_type": conversation.booking_room_preference,
                "response_scope": "room_details",
            }
        else:
            return None
        await self._execute_hotel_tool(conversation.id, tool_name, arguments)
        say = self.booking_coordinator.verified_room_catalog_response(conversation)
        if not say:
            return None
        self._remember_turn_state(conversation, intent="booking", topic="booking")
        await self.store.add_turn(conversation.id, "assistant", say)
        await self.session.commit()
        return HotelTurnResponse(conversation.id, "booking", say)

    @staticmethod
    def _request_rule_from_interpretation(
        interpretation: HotelTurnInterpretation | None,
    ) -> tuple[str, str, str, tuple[str, ...]] | None:
        if not interpretation:
            return None
        if interpretation.action not in {"create_request", "ask_slot", "clarify", "confirm"}:
            return None
        category = interpretation.service_category or (
            interpretation.intent
            if interpretation.intent
            in {"room_service", "housekeeping", "maintenance", "security", "concierge"}
            else None
        )
        if not category:
            return None
        routing = {
            "maintenance": ("engineering", "urgent"),
            "housekeeping": ("housekeeping", "routine"),
            "concierge": ("concierge", "routine"),
            "room_service": ("food_beverage", "routine"),
            "security": ("security", "emergency"),
        }
        department, priority = routing[category]
        return department, priority, category, ()

    @staticmethod
    def _booking_update_from_interpretation(
        interpretation: HotelTurnInterpretation | None,
        *,
        caller_text: str,
        expected_date_slot: str | None = None,
    ) -> BookingSlotUpdate:
        if not interpretation:
            return BookingSlotUpdate()

        def parse_date(value: str | None) -> date | None:
            if not value:
                return None
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None

        local_dates = HotelConversationService._extract_booking_slot_update(
            caller_text,
            today=date.today(),
            expected_date_slot=expected_date_slot,
        )

        evidence = validate_guest_count_evidence(
            caller_text,
            guest_count=interpretation.booking_guest_count,
            evidence=interpretation.booking_guest_count_evidence,
        )
        if evidence.status == "unknown":
            # Groq's JSON mode can validly omit an optional field even when
            # the transcript contains an explicit Hindi/Hinglish party size.
            # Recover only a directly evidenced count; never infer from room
            # capacity, recommendations, or the next question.
            local_evidence = explicit_guest_count_evidence_from_text(caller_text)
            if local_evidence.status == "explicit":
                evidence = local_evidence
        model_check_in = parse_date(interpretation.booking_check_in)
        model_check_out = parse_date(interpretation.booking_check_out)
        if expected_date_slot == "check_out" and local_dates.check_out:
            # A bare date answering an active checkout prompt must not be
            # reused as a correction to the already accepted check-in date.
            model_check_in = None
            model_check_out = local_dates.check_out

        check_in = local_dates.check_in or model_check_in
        check_out = local_dates.check_out or model_check_out
        # Once both dates are known, their difference is the only valid
        # duration.  A model may extract the dates correctly yet hallucinate a
        # stay length, as happened with a 13th-to-15th request becoming six
        # nights.  Keep a model-provided duration only while a date is still
        # missing.
        nights = (
            (check_out - check_in).days
            if check_in and check_out and check_out > check_in
            else interpretation.booking_nights
        )

        return BookingSlotUpdate(
            # Caller-verifiable date text outranks a model-normalized date.
            # This prevents a malformed model date from replacing an explicit
            # Hindi/Hinglish date the caller actually said.
            check_in=check_in,
            check_out=check_out,
            # Verbatim evidence can be committed as a new fact. Semantic
            # evidence is retained as a candidate and requires confirmation.
            guest_count=(
                evidence.guest_count
                if evidence.status in {"explicit", "semantic"}
                else None
            ),
            guest_count_evidence=evidence.evidence,
            guest_count_evidence_status=evidence.status,
            nights=nights,
        )

    @staticmethod
    def _booking_fact_ledger(conversation) -> BookingFactLedger:
        """Load the ledger and backfill active facts from older scalar rows."""

        ledger = BookingFactLedger.from_json(conversation.booking_facts_json)
        legacy_values = (
            ("check_in", conversation.booking_check_in.isoformat() if conversation.booking_check_in else None),
            ("check_out", conversation.booking_check_out.isoformat() if conversation.booking_check_out else None),
            ("guest_count", conversation.booking_guest_count),
            ("nights", conversation.booking_nights),
            ("room_preference", conversation.booking_room_preference),
            ("booking_plan", conversation.booking_plan_json),
            ("criteria", conversation.booking_criteria_json),
        )
        for field, value in legacy_values:
            if value is not None and ledger.active(field) is None:
                ledger.propose(
                    field,
                    value,
                    evidence="legacy persisted booking state",
                    source_turn_id=None,
                    evidence_status="legacy",
                )
        return ledger

    @staticmethod
    def _sync_booking_fact_projection(conversation, ledger: BookingFactLedger) -> None:
        """Persist the ledger and project only active facts to legacy columns."""

        values = ledger.active_values()
        if isinstance(values.get("check_in"), str):
            try:
                conversation.booking_check_in = date.fromisoformat(values["check_in"])
            except ValueError:
                pass
        if isinstance(values.get("check_out"), str):
            try:
                conversation.booking_check_out = date.fromisoformat(values["check_out"])
            except ValueError:
                pass
        if isinstance(values.get("guest_count"), int):
            conversation.booking_guest_count = values["guest_count"]
        if isinstance(values.get("nights"), int):
            conversation.booking_nights = values["nights"]
        if isinstance(values.get("room_preference"), str):
            conversation.booking_room_preference = values["room_preference"]
        if isinstance(values.get("criteria"), str):
            conversation.booking_criteria_json = values["criteria"]
            criteria = BookingCriteria.from_json(values["criteria"])
            conversation.booking_criteria_revision = criteria.revision
        conversation.booking_facts_json = ledger.to_json()
        conversation.booking_fact_revision = ledger.revision

    @staticmethod
    def _fact_label(field: str) -> str:
        return {
            "check_in": "check-in date",
            "check_out": "check-out date",
            "guest_count": "guest count",
            "nights": "number of nights",
            "room_preference": "room type",
            "booking_plan": "room layout",
            "criteria": "room requirements",
        }.get(field, field.replace("_", " "))

    @staticmethod
    def _fact_value_label(field: str, value: Any) -> str:
        if field == "guest_count":
            return f"{value} guests"
        if field == "nights":
            return f"{value} night{'s' if value != 1 else ''}"
        if field == "room_preference":
            return str(value).replace("_", " ").title()
        if field == "booking_plan":
            plan = BookingPlan.from_json(value if isinstance(value, str) else None)
            parts: list[str] = []
            if plan.requested_room_count is not None:
                parts.append(
                    f"{plan.requested_room_count} room"
                    f"{'s' if plan.requested_room_count != 1 else ''}"
                )
            if plan.party_groups:
                couple_count = sum(
                    group.count
                    for group in plan.party_groups
                    if group.label.casefold() == "couple"
                )
                if couple_count:
                    parts.append(
                        f"{couple_count} couple"
                        f"{'s' if couple_count != 1 else ''} together"
                    )
            return " with ".join(parts) if parts else "the requested room layout"
        return str(value)

    @staticmethod
    def _booking_plan_signature(value: str | None) -> tuple[Any, ...]:
        """Compare layout meaning without treating JSON formatting as a change."""

        plan = BookingPlan.from_json(value)
        return (
            tuple(
                (group.label, group.people_per_group, group.count)
                for group in plan.party_groups
            ),
            plan.room_count,
            tuple((item.room_type, item.quantity) for item in plan.allocation),
            plan.allow_mixed_types,
            plan.allow_room_sharing,
            plan.party_composition_known,
        )

    def _booking_fact_confirmation_prompt(
        self,
        proposals: list[FactProposal],
    ) -> str | None:
        pending = [
            proposal
            for proposal in proposals
            if proposal.outcome in {"pending_confirmation", "ambiguous"}
        ]
        if not pending:
            return None
        prompts: list[str] = []
        for proposal in pending[:3]:
            field = self._fact_label(proposal.field)
            if proposal.outcome == "ambiguous":
                prompts.append(
                    self.language_adapter.text(
                        "booking_fact_ambiguous",
                        field=field,
                        first_value=self._fact_value_label(
                            proposal.field, proposal.pending_value
                        ),
                        second_value=self._fact_value_label(
                            proposal.field, proposal.proposed_value
                        ),
                    )
                )
            elif proposal.current_value is None:
                prompts.append(
                    self.language_adapter.text(
                        "booking_fact_initial_confirmation",
                        field=field,
                        new_value=self._fact_value_label(
                            proposal.field, proposal.proposed_value
                        ),
                    )
                )
            else:
                prompts.append(
                    self.language_adapter.text(
                        "booking_fact_correction",
                        field=field,
                        old_value=self._fact_value_label(
                            proposal.field, proposal.current_value
                        ),
                        new_value=self._fact_value_label(
                            proposal.field, proposal.proposed_value
                        ),
                    )
                )
        return " ".join(prompts)

    def _stage_booking_fact_changes(
        self,
        conversation,
        update: BookingSlotUpdate,
        *,
        caller_text: str,
        source_turn_id: str,
        interpretation: HotelTurnInterpretation | None = None,
        selected_room: bool = False,
        selected_plan: bool = False,
    ) -> tuple[BookingSlotUpdate, list[FactProposal]]:
        """Stage caller facts and return only values safe for this turn's flow."""

        ledger = self._booking_fact_ledger(conversation)
        accepted: dict[str, Any] = {}
        proposals: list[FactProposal] = []

        def stage(
            field: str,
            value: Any,
            *,
            evidence: str | None = caller_text,
            evidence_status: str = "explicit",
        ) -> None:
            proposal = ledger.propose(
                field,
                value,
                evidence=evidence,
                source_turn_id=source_turn_id,
                evidence_status=evidence_status,
            )
            proposals.append(proposal)
            if proposal.outcome in {"accepted", "unchanged"}:
                accepted[field] = value

        stage("check_in", update.check_in.isoformat()) if update.check_in else None
        stage("check_out", update.check_out.isoformat()) if update.check_out else None
        if update.guest_count is not None:
            stage(
                "guest_count",
                update.guest_count,
                evidence=update.guest_count_evidence or caller_text,
                evidence_status=update.guest_count_evidence_status
                if update.guest_count_evidence_status != "unknown"
                else "explicit",
            )
        if update.nights is not None:
            stage("nights", update.nights)
        if (
            selected_room
            and interpretation
            and interpretation.booking_room_preference
        ):
            stage("room_preference", interpretation.booking_room_preference)
        if selected_plan and interpretation and interpretation.booking_plan_json:
            booking_plan_value = interpretation.booking_plan_json
            try:
                model_plan = json.loads(booking_plan_value)
            except json.JSONDecodeError:
                model_plan = None
            if isinstance(model_plan, dict) and model_plan.get("room_count") is None:
                caller_plan = BookingPlan.from_json(conversation.booking_plan_json)
                caller_plan.apply_caller_turn(caller_text, [])
                if caller_plan.requested_room_count is not None:
                    # The semantic parser may describe the couple/group but
                    # omit the separate room quantity from the same turn.
                    # Preserve the caller-owned quantity before this plan is
                    # staged as a pending fact.
                    model_plan["room_count"] = caller_plan.requested_room_count
                    model_plan["room_count_evidence"] = caller_text
                    booking_plan_value = json.dumps(model_plan, sort_keys=True)
            if self._booking_plan_signature(
                conversation.booking_plan_json
            ) != self._booking_plan_signature(booking_plan_value):
                stage("booking_plan", booking_plan_value)

        self._sync_booking_fact_projection(conversation, ledger)
        return (
            BookingSlotUpdate(
                check_in=accepted.get("check_in")
                and date.fromisoformat(accepted["check_in"]),
                check_out=accepted.get("check_out")
                and date.fromisoformat(accepted["check_out"]),
                guest_count=accepted.get("guest_count"),
                nights=accepted.get("nights"),
                guest_count_evidence=update.guest_count_evidence
                if "guest_count" in accepted
                else None,
                guest_count_evidence_status=update.guest_count_evidence_status,
            ),
            proposals,
        )

    async def _resolve_pending_booking_facts(
        self,
        conversation,
        interpretation: HotelTurnInterpretation | None,
        normalized: str,
        *,
        controller_decision: ControllerDecision | None = None,
    ) -> HotelTurnResponse | None:
        """Resolve a pending correction before offer acceptance or new work."""

        ledger = self._booking_fact_ledger(conversation)
        if not ledger.has_pending():
            return None
        # The controller owns the semantic decision when it is available.
        # Legacy text/model heuristics remain only as a compatibility adapter
        # for callers that invoke this private seam without a controller
        # decision (or for an interpreter that cannot emit commands).
        if controller_decision and controller_decision.action == "accept_pending_fact":
            affirmative = True
            negative = False
        elif controller_decision and controller_decision.action == "reject_pending_fact":
            affirmative = False
            negative = True
        else:
            affirmative = self._turn_accepts(
                interpretation,
                normalized,
                confirmation_context=True,
            )
            negative = (
                self._interpretation_rejects(interpretation)
                if self.interpreter
                else self._is_negative(normalized)
            )
        if not affirmative and not negative:
            return None

        fields = ledger.pending_fields()
        results = [ledger.confirm(field, accepted=affirmative) for field in fields]
        self._sync_booking_fact_projection(conversation, ledger)
        if affirmative:
            active_plan = ledger.active_value("booking_plan")
            if isinstance(active_plan, str):
                conversation.booking_plan_json = active_plan
                conversation.booking_plan_version += 1
        if affirmative:
            # A confirmed correction changes the facts an offer or hold was
            # based on. Keep the old scalar out of downstream searches.
            if conversation.booking_hold_reservation_id:
                await HotelPMSAdapter(self.session, self.hotel_id).cancel_booking_hold(
                    conversation.booking_hold_reservation_id
                )
                conversation.booking_hold_reservation_id = None
            if conversation.booking_offer_id or conversation.booking_last_search_result:
                await HotelBookingOfferService(
                    self.session, self.hotel_id, telemetry=self.telemetry
                ).invalidate_presented_offers(
                    conversation,
                    reason="The caller confirmed a corrected booking fact.",
                    commit=False,
                )
            conversation.booking_last_search_result = None
            conversation.booking_catalog_snapshot_version = None
            conversation.booking_catalog_relevant_json = None
            say = " ".join(
                self.language_adapter.text(
                    "booking_fact_confirmed",
                    field=self._fact_label(result.field),
                    new_value=self._fact_value_label(result.field, result.proposed_value),
                )
                for result in results
            )
            progression_prompt = self.booking_coordinator.progressive_booking_prompt(
                conversation
            )
            if progression_prompt:
                say = f"{say} {progression_prompt}"
        else:
            say = " ".join(
                self.language_adapter.text(
                    "booking_fact_rejected",
                    field=self._fact_label(result.field),
                    old_value=self._fact_value_label(result.field, result.current_value),
                )
                for result in results
            )
        conversation.pending_category = "booking"
        conversation.active_topic = "booking"
        conversation.state = "collecting_booking_allocation"
        await self.store.add_turn(conversation.id, "assistant", say)
        await self.session.commit()
        return HotelTurnResponse(conversation.id, "booking", say)

    @staticmethod
    def _is_initial_party_composition_update(
        conversation,
        interpretation: HotelTurnInterpretation | None,
    ) -> bool:
        """Keep a caller's first couples answer out of the correction ledger."""

        if not interpretation or not interpretation.booking_plan_json:
            return False
        current_plan = BookingPlan.from_json(conversation.booking_plan_json)
        if current_plan.party_composition_known:
            return False
        try:
            raw = json.loads(interpretation.booking_plan_json)
        except json.JSONDecodeError:
            return False
        if not isinstance(raw, dict):
            return False
        return (
            "party_composition_known" in raw
            and set(raw).issubset({"party_composition_known", "party_groups"})
        )

    async def _apply_interpreted_booking_plan(
        self,
        conversation,
        interpretation: HotelTurnInterpretation | None,
        *,
        caller_text: str,
    ) -> bool:
        """Apply only a validated, caller-explicit model allocation update.

        The dialogue kernel is deliberately called before JSON parsing.  A
        syntactically valid layout from the model is still only a proposal if
        the caller was asking a question or comparing alternatives.
        """

        if not interpretation or not interpretation.booking_plan_json:
            return False
        catalog = await HotelPMSAdapter(self.session, self.hotel_id).room_types()
        caller_plan = BookingPlan.from_json(conversation.booking_plan_json)
        caller_plan.apply_caller_turn(caller_text, catalog)
        decision = self.dialogue_controller.assess_turn(
            interpretation,
            has_active_offer=bool(conversation.booking_offer_id),
            has_search_evidence=bool(conversation.booking_last_search_result),
            caller_has_explicit_layout=(
                caller_plan.has_exact_allocation
                or caller_plan.requested_room_count is not None
                or caller_plan.allow_mixed_types
            ),
        )
        if not decision.can_update_booking_plan:
            await self._record_audit(
                conversation.id,
                "booking_plan_rejected",
                {
                    "reason": decision.reason,
                    "dialogue_act": decision.act,
                    "caller_selected_option": decision.caller_selected_option,
                    "caller_text": caller_text,
                },
            )
            return False
        if interpretation.intent != "booking" and conversation.pending_category != "booking":
            return False
        try:
            raw = json.loads(interpretation.booking_plan_json)
        except json.JSONDecodeError:
            return False
        if not isinstance(raw, dict):
            return False

        if not self._model_plan_matches_caller(raw, caller_plan, caller_text):
            await self._record_audit(
                conversation.id,
                "booking_plan_rejected",
                {
                    "reason": "model_plan_contains_unspoken_numeric_or_layout_fact",
                    "caller_text": caller_text,
                },
            )
            return False

        raw_allocation = raw.get("room_allocation")
        allocation: list[dict[str, Any]] | None = None
        if isinstance(raw_allocation, list):
            canonical_types = {room_type.casefold(): room_type for room_type in catalog}
            allocation = []
            for item in raw_allocation:
                if not isinstance(item, dict):
                    return False
                room_type = item.get("room_type") or item.get("type")
                if not isinstance(room_type, str):
                    return False
                canonical = canonical_types.get(room_type.casefold())
                if not canonical:
                    return False
                allocation.append({**item, "room_type": canonical})

        party_groups = raw.get("party_groups")
        if party_groups is not None and not isinstance(party_groups, list):
            return False
        allocation_mode = raw.get("allocation_mode")
        if allocation_mode is not None and not isinstance(allocation_mode, str):
            return False
        party_composition_known = raw.get("party_composition_known")
        if party_composition_known is not None and not isinstance(
            party_composition_known, bool
        ):
            return False
        if (
            allocation is None
            and party_groups is None
            and raw.get("room_count") is None
            and allocation_mode is None
            and party_composition_known is None
        ):
            return False

        plan = BookingPlan.from_json(conversation.booking_plan_json)
        # Preserve a caller-explicit room quantity even when the model returns
        # only the related party composition.  The model may omit optional
        # layout fields; it must not erase a fact the caller just supplied.
        room_count = raw.get("room_count")
        if room_count is None:
            room_count = caller_plan.requested_room_count
        if not plan.apply_model_update(
            party_groups=party_groups,
            room_count=room_count,
            room_allocation=allocation,
            allocation_mode=allocation_mode,
            party_composition_known=party_composition_known,
        ):
            return False
        if (
            plan.party_composition_known
            and plan.party_size
            and conversation.booking_guest_count
            and plan.party_size > conversation.booking_guest_count
        ):
            await self._record_audit(
                conversation.id,
                "booking_plan_rejected",
                {
                    "reason": "party_groups_exceed_guest_count",
                    "party_size": plan.party_size,
                    "guest_count": conversation.booking_guest_count,
                    "caller_text": caller_text,
                },
            )
            return False
        if (
            plan.allow_room_sharing is False
            and plan.requested_room_count is not None
            and len(plan.occupancies) != plan.requested_room_count
        ):
            # "Separate" means every declared party group needs its own
            # selected room. Reject a malformed model plan before it can
            # replace a valid PMS recommendation or reach offer creation.
            return False
        if conversation.booking_hold_reservation_id:
            await HotelPMSAdapter(self.session, self.hotel_id).cancel_booking_hold(
                conversation.booking_hold_reservation_id
            )
            conversation.booking_hold_reservation_id = None
        await HotelBookingOfferService(
            self.session, self.hotel_id, telemetry=self.telemetry
        ).invalidate_presented_offers(
            conversation,
            reason="The caller selected a different room layout.",
            commit=False,
        )
        conversation.booking_plan_json = plan.to_json()
        conversation.booking_plan_version = plan.version
        conversation.booking_last_search_result = None
        conversation.state = "collecting_booking_allocation"
        return True

    @staticmethod
    def _model_plan_matches_caller(
        raw: dict[str, Any],
        caller_plan: BookingPlan,
        caller_text: str,
    ) -> bool:
        """Allow model plan values only when the model supplies transcript evidence."""

        if "room_count" in raw:
            try:
                room_count = int(raw["room_count"])
            except (TypeError, ValueError):
                return False
            if not 1 <= room_count <= 100:
                return False
            evidence = raw.get("room_count_evidence")
            if not isinstance(evidence, str) or not evidence.strip():
                return False

            def normalize(value: str) -> str:
                return " ".join(
                    unicodedata.normalize("NFKC", value).casefold().split()
                )

            if normalize(evidence) not in normalize(caller_text):
                return False
            if (
                caller_plan.requested_room_count is not None
                and caller_plan.requested_room_count != room_count
            ):
                return False

        if "room_allocation" in raw:
            model_allocation = raw.get("room_allocation")
            if not isinstance(model_allocation, list) or not caller_plan.allocation:
                return False
            normalized_model: list[tuple[str, int]] = []
            for item in model_allocation:
                if not isinstance(item, dict):
                    return False
                room_type = item.get("room_type") or item.get("type")
                quantity = item.get("quantity") or item.get("count")
                try:
                    normalized_model.append((str(room_type).casefold(), int(quantity)))
                except (TypeError, ValueError):
                    return False
            expected = [
                (item.room_type.casefold(), item.quantity)
                for item in caller_plan.allocation
            ]
            if normalized_model != expected:
                return False

        if "party_groups" in raw:
            model_groups = raw.get("party_groups")
            if not isinstance(model_groups, list):
                return False
            normalized_model = []
            for item in model_groups:
                if not isinstance(item, dict):
                    return False
                try:
                    normalized_model.append(
                        (
                            str(item.get("label") or item.get("kind") or "group"),
                            int(item.get("people_per_group") or item.get("size")),
                            int(item.get("count", 1)),
                        )
                    )
                except (TypeError, ValueError):
                    return False
            if caller_plan.party_groups and normalized_model != [
                (group.label, group.people_per_group, group.count)
                for group in caller_plan.party_groups
            ] and not (
                not normalized_model and raw.get("party_composition_known") is True
            ):
                # A semantic model extraction may understand Hinglish or an
                # ASR variant such as "teen couples" even when the small
                # legacy fallback parser does not. The domain validator still
                # checks positive sizes and the active guest total below.
                return False
            if not normalized_model and raw.get("party_composition_known") is not True:
                return False

        if "allocation_mode" in raw:
            mode = raw.get("allocation_mode")
            supported = {
                "mixed": caller_plan.allow_mixed_types,
                "shared": caller_plan.allow_room_sharing is True,
                "separate": caller_plan.allow_room_sharing is False,
            }
            if not isinstance(mode, str) or not supported.get(mode.casefold(), False):
                return False

        return any(
            key in raw
            for key in (
                "party_groups",
                "room_count",
                "room_allocation",
                "allocation_mode",
                "party_composition_known",
            )
        )

    @staticmethod
    def _interpretation_response(interpretation: HotelTurnInterpretation | None) -> str | None:
        if not interpretation:
            return None
        return interpretation.response

    async def _return_interpreter_response(
        self,
        conversation,
        interpretation: HotelTurnInterpretation,
    ) -> HotelTurnResponse | None:
        """Commit the current turn's response without replaying old evidence.

        A persisted PMS result is context for later turns. It is allowed to
        replace model prose only when the PMS lookup happened during this same
        turn; otherwise the newest caller question must reach the response.
        """

        verified_response: str | None = None
        if self._booking_lookup_completed_this_turn:
            verified_response = await self.booking_coordinator.recommended_offer_response(
                conversation
            )
            if not verified_response:
                verified_response = self.booking_coordinator.verified_room_search_response(
                    conversation
                )
            if not verified_response:
                verified_response = self.booking_coordinator.verified_room_catalog_response(
                    conversation
                )
        model_response = select_turn_response(
            interpretation=interpretation,
            verified_response=verified_response,
            verified_evidence_is_current=self._booking_lookup_completed_this_turn,
        )
        if not model_response:
            return None
        self._remember_turn_state(
            conversation,
            intent=interpretation.intent,
            topic=interpretation.intent,
            confidence=interpretation.confidence,
        )
        await self.store.add_turn(conversation.id, "assistant", model_response)
        await self.session.commit()
        return HotelTurnResponse(conversation.id, interpretation.intent, model_response)

    async def _return_verified_lookup_response(self, conversation) -> HotelTurnResponse | None:
        """Speak current PMS evidence when the model summary could not be parsed."""

        if not self._booking_lookup_completed_this_turn:
            return None
        say = await self.booking_coordinator.recommended_offer_response(conversation)
        if not say:
            say = self.booking_coordinator.verified_room_search_response(conversation)
        if not say:
            say = self.booking_coordinator.verified_room_catalog_response(conversation)
        if not say:
            return None
        self._remember_turn_state(conversation, intent="booking", topic="booking")
        await self.store.add_turn(conversation.id, "assistant", say)
        await self.session.commit()
        await self._record_audit(
            conversation.id,
            "llm_format_fallback",
            {"response_source": "verified_pms_lookup"},
        )
        return HotelTurnResponse(conversation.id, "booking", say)

    async def _return_interpreter_failure(self, conversation) -> HotelTurnResponse:
        """Expose interpreter/tool failure instead of silently using a controller."""

        say = self.language_adapter.text("understand_failure")
        await self.store.add_turn(conversation.id, "assistant", say)
        await self.session.commit()
        return HotelTurnResponse(conversation.id, "interpreter_unavailable", say)

    @staticmethod
    def _remember_turn_state(
        conversation,
        *,
        intent: str,
        topic: str | None = None,
        confidence: float | None = None,
    ) -> None:
        """Persist the conversation state that the next turn should inherit."""

        conversation.last_intent = intent
        conversation.last_turn_confidence = confidence
        if topic:
            conversation.active_topic = topic

    @staticmethod
    def _workflow_label(category: str | None) -> str:
        labels = {
            "booking": "room booking",
            "room_service": "room-service request",
            "maintenance": "maintenance request",
            "housekeeping": "housekeeping request",
            "security": "security request",
        }
        return labels.get(category or "", "earlier request")

    @staticmethod
    def _clear_deferred_workflow(conversation) -> None:
        conversation.deferred_category = None
        conversation.deferred_description = None
        conversation.deferred_state = None
        conversation.deferred_turns = 0

    def _suspend_active_workflow(self, conversation, *, incoming_category: str) -> str:
        """Suspend one ordinary workflow before starting a different one.

        The return value keeps the turn arbiter simple: callers either continue
        normally, receive a bounded-queue clarification, or start the new task
        after the active one is safely retained.
        """

        if not conversation.pending_category or conversation.pending_category == incoming_category:
            return "unchanged"
        if conversation.deferred_category:
            return "full"
        conversation.deferred_category = conversation.pending_category
        conversation.deferred_description = conversation.pending_description
        conversation.deferred_state = conversation.state
        conversation.deferred_turns = 0
        conversation.pending_category = None
        conversation.pending_description = None
        conversation.state = "ready"
        return "suspended"

    def _offer_deferred_resume(self, conversation) -> str:
        """Keep a completed side task from silently reviving old work."""

        if not conversation.deferred_category:
            return ""
        conversation.state = "awaiting_deferred_resume"
        return (
            f" I still have your {self._workflow_label(conversation.deferred_category)} open. "
            "Would you like to continue it?"
        )

    def _resume_deferred_workflow(self, conversation) -> str | None:
        category = conversation.deferred_category
        if not category:
            return None
        conversation.pending_category = category
        conversation.pending_description = conversation.deferred_description
        conversation.state = conversation.deferred_state or "ready"
        self._clear_deferred_workflow(conversation)
        return category

    def _deferred_resume_prompt(self, category: str) -> str:
        prompts = {
            "booking": (
                "Let's continue your room booking. Would you like to review the available options or choose a room type?"
            ),
            "room_service": "Let's continue your room-service request. What would you like to order?",
            "maintenance": "Let's continue your maintenance request. Is the issue still happening?",
            "housekeeping": "Let's continue your housekeeping request. What do you need?",
            "security": "Let's continue your security request. What do you need help with?",
        }
        return prompts.get(
            category, "Let's continue the earlier request. What would you like to do?"
        )

    async def start(self, caller_phone: str) -> tuple[UUID, str]:
        hotel = await self.store.hotel(self.hotel_id)
        if not hotel:
            raise HotelConversationNotFoundError("Hotel not found")
        conversation = await self.store.start_conversation(self.hotel_id, caller_phone)
        if conversation.caller_name and conversation.reservation_id:
            greeting = self.language_adapter.text(
                "welcome_back",
                caller_name=conversation.caller_name,
            )
        else:
            greeting = self.language_adapter.text("greeting", hotel=hotel.name)
        await self.store.add_turn(conversation.id, "assistant", greeting)
        await self.session.commit()
        return conversation.id, greeting

    async def handle_turn(
        self,
        conversation_id: UUID,
        text: str,
        *,
        on_response: Callable[[str], Awaitable[None]] | None = None,
    ) -> HotelTurnResponse:
        conversation = await self.store.conversation(conversation_id)
        if not conversation:
            raise HotelConversationNotFoundError("Conversation not found")
        self._booking_lookup_completed_this_turn = False
        self._party_composition_required_context = None
        user_turn = await self.store.add_turn(conversation.id, "user", text)
        turn_id = str(user_turn.id)
        if not self.interpreter and not self.allow_legacy_language_rules:
            say = self.language_adapter.text("interpreter_unavailable")
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "interpreter_unavailable", say)
        normalized = " ".join(text.lower().split())
        contextual_room_type = await self._handle_active_booking_room_type(
            conversation,
            text,
            on_response=on_response,
        )
        if contextual_room_type:
            return contextual_room_type
        interpretation = await self._interpret_turn(
            conversation,
            text,
            turn_id=turn_id,
            on_response=on_response,
        )
        if self.interpreter and interpretation is None:
            if self._party_composition_required_context:
                check_in, check_out, guest_count = self._party_composition_required_context
                booking_update = BookingSlotUpdate(
                    check_in=check_in,
                    check_out=check_out,
                    guest_count=guest_count,
                    guest_count_evidence=f"{guest_count} guests",
                    guest_count_evidence_status="explicit",
                )
                self._stage_booking_fact_changes(
                    conversation,
                    booking_update,
                    caller_text=text,
                    source_turn_id=turn_id,
                )
                if not conversation.booking_check_out:
                    conversation.booking_check_out = conversation.booking_check_in + timedelta(
                        days=conversation.booking_nights or 0
                    )
                say = self.language_adapter.text(
                    "booking_party_composition",
                    guest_count=guest_count,
                )
                conversation.pending_category = "booking"
                conversation.active_topic = "booking"
                conversation.state = "collecting_booking_allocation"
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "booking", say)
            verified_response = await self._return_verified_lookup_response(conversation)
            if verified_response:
                return verified_response
            # A provider failure must not erase an explicit booking fact from
            # the caller's transcript. Recover only locally verifiable slots,
            # then let the normal controller/reducer choose the next step.
            fallback_update = self._extract_booking_slot_update(
                text, today=date.today()
            )
            local_guest_evidence = explicit_guest_count_evidence_from_text(text)
            if local_guest_evidence.status == "explicit":
                fallback_update = BookingSlotUpdate(
                    check_in=fallback_update.check_in,
                    check_out=fallback_update.check_out,
                    guest_count=local_guest_evidence.guest_count,
                    guest_count_evidence=local_guest_evidence.evidence,
                    guest_count_evidence_status="explicit",
                    nights=fallback_update.nights,
                )
            if fallback_update.has_value:
                interpretation = HotelTurnInterpretation(
                    intent="booking",
                    action="ask_slot",
                    dialogue_act="ask",
                    confidence=1.0,
                    booking_check_in=(
                        fallback_update.check_in.isoformat()
                        if fallback_update.check_in
                        else None
                    ),
                    booking_check_out=(
                        fallback_update.check_out.isoformat()
                        if fallback_update.check_out
                        else None
                    ),
                    booking_guest_count=fallback_update.guest_count,
                    booking_guest_count_evidence=fallback_update.guest_count_evidence,
                    booking_nights=fallback_update.nights,
                )
                await self._record_audit(
                    conversation.id,
                    "interpreter_failure_recovered",
                    {
                        "recovered_slots": {
                            "check_in": (
                                fallback_update.check_in.isoformat()
                                if fallback_update.check_in
                                else None
                            ),
                            "check_out": (
                                fallback_update.check_out.isoformat()
                                if fallback_update.check_out
                                else None
                            ),
                            "guest_count": fallback_update.guest_count,
                            "nights": fallback_update.nights,
                        }
                    },
                )
            if interpretation is None:
                plan = BookingPlan.from_json(conversation.booking_plan_json)
                if (
                    conversation.booking_guest_count
                    and conversation.booking_guest_count > 1
                    and not plan.party_composition_known
                ):
                    # Both parser attempts failed, but the booking state tells
                    # us exactly which fact is still needed. Do not describe
                    # that provider failure as caller confusion.
                    say = self.language_adapter.text(
                        "booking_party_composition",
                        guest_count=conversation.booking_guest_count,
                    )
                    conversation.pending_category = "booking"
                    conversation.active_topic = "booking"
                    conversation.state = "collecting_booking_allocation"
                    await self.store.add_turn(conversation.id, "assistant", say)
                    await self.session.commit()
                    return HotelTurnResponse(conversation.id, "booking", say)
                say = self.language_adapter.text("understand_failure")
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "interpreter_unavailable", say)
        if self.interpreter and interpretation and interpretation.tool_error:
            return await self._return_interpreter_failure(conversation)

        controller_decision = self.dialogue_controller.decide_turn(
            DialogueSnapshot(
                state=conversation.state,
                pending_category=conversation.pending_category,
                has_active_offer=bool(conversation.booking_offer_id),
                has_search_evidence=bool(conversation.booking_last_search_result),
                has_booking_dates=bool(
                    conversation.booking_check_in and conversation.booking_check_out
                ),
                has_guest_count=bool(conversation.booking_guest_count),
                has_room_preference=bool(conversation.booking_room_preference),
                has_pending_fact_confirmation=self._booking_fact_ledger(
                    conversation
                ).has_pending(),
            ),
            interpretation,
        )
        await self._record_audit(
            conversation.id,
            "controller_decision",
            {
                "command": controller_decision.command,
                "action": controller_decision.action,
                "reason": controller_decision.reason,
                "mutates_booking_state": controller_decision.mutates_booking_state,
                "requires_tool": controller_decision.requires_tool,
            },
        )

        pending_fact_response = await self._resolve_pending_booking_facts(
            conversation,
            interpretation,
            normalized,
            controller_decision=controller_decision,
        )
        if pending_fact_response:
            return pending_fact_response

        if controller_decision.action == "resume_booking_prompt":
            progression_prompt = self.booking_coordinator.progressive_booking_prompt(
                conversation
            )
            if progression_prompt:
                conversation.pending_category = "booking"
                conversation.active_topic = "booking"
                conversation.state = "collecting_booking_allocation"
                await self.store.add_turn(
                    conversation.id, "assistant", progression_prompt
                )
                await self.session.commit()
                return HotelTurnResponse(
                    conversation.id,
                    "booking",
                    progression_prompt,
                )

        if conversation.state == "awaiting_post_action_followup":
            if self.interpreter:
                if self._turn_rejects(
                    interpretation,
                    normalized,
                    confirmation_context=True,
                ):
                    conversation.state = "ready"
                    say = self.language_adapter.text("post_action_closed")
                    await self.store.add_turn(conversation.id, "assistant", say)
                    await self.session.commit()
                    return HotelTurnResponse(conversation.id, "conversation_closed", say)
                if self._turn_accepts(
                    interpretation,
                    normalized,
                    confirmation_context=True,
                ):
                    conversation.state = "ready"
                    say = self.language_adapter.text("post_action_continue")
                    await self.store.add_turn(conversation.id, "assistant", say)
                    await self.session.commit()
                    return HotelTurnResponse(conversation.id, "conversation", say)
            elif self._is_negative(normalized):
                conversation.state = "ready"
                say = self.language_adapter.text("post_action_closed")
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "conversation_closed", say)
            elif self._is_affirmative(normalized):
                conversation.state = "ready"
                say = self.language_adapter.text("post_action_continue")
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "conversation", say)
            # A new substantive request should continue through the normal
            # hotel router instead of being trapped by the closing prompt.
            conversation.state = "ready"

        confirmation_context = (
            conversation.pending_category == "booking"
            and conversation.state == "awaiting_booking_confirmation"
        )
        if (
            controller_decision.action == "reject_offer"
            and confirmation_context
        ):
            result = await self.booking_coordinator.cancel_unfinished_booking(conversation)
            return self._booking_response(conversation, result)

        if controller_decision.action == "accept_offer":
            result = await self.booking_coordinator.accept_current_offer(conversation)
            return self._booking_response(conversation, result)

        # Compatibility recovery for an interpreter that returned an
        # otherwise valid turn but missed the semantic meaning of a short
        # confirmation such as "ji" or "nahi ji". The controller remains the
        # primary authority; this bounded fallback is scoped to the active
        # booking confirmation and cannot create a hold without that state.
        if confirmation_context:
            if self._turn_rejects(
                interpretation,
                normalized,
                confirmation_context=True,
            ):
                result = await self.booking_coordinator.cancel_unfinished_booking(
                    conversation
                )
                await self._record_audit(
                    conversation.id,
                    "controller_compatibility_recovery",
                    {"action": "reject_offer", "reason": "legacy_confirmation_fallback"},
                )
                return self._booking_response(conversation, result)
            acceptance = self.booking_flow.acceptance_decision(
                self.booking_flow.lifecycle_snapshot(conversation),
                affirmative=self._turn_accepts(
                    interpretation,
                    normalized,
                    confirmation_context=True,
                ),
            )
            if acceptance.action == "accept_offer":
                result = await self.booking_coordinator.accept_current_offer(conversation)
                await self._record_audit(
                    conversation.id,
                    "controller_compatibility_recovery",
                    {"action": "accept_offer", "reason": "legacy_confirmation_fallback"},
                )
                return self._booking_response(conversation, result)
        if self.interpreter and interpretation and interpretation.read_only_tools:
            # A completed lookup can only inform speech. It cannot be used as
            # authority for a write or be handed to a separate response
            # controller that might replace what the lookup found.
            interpretation = interpretation.model_copy(update={"action": "answer"})
        dialogue_decision = self.dialogue_controller.assess_turn(
            interpretation,
            has_active_offer=bool(conversation.booking_offer_id),
            has_search_evidence=bool(conversation.booking_last_search_result),
        )
        await self._record_audit(
            conversation.id,
            "dialogue_decision",
            {
                "caller_text": text,
                "dialogue_act": dialogue_decision.act,
                "caller_selected_option": dialogue_decision.caller_selected_option,
                "can_update_booking_plan": dialogue_decision.can_update_booking_plan,
                "should_refresh_availability": dialogue_decision.should_refresh_availability,
                "reason": dialogue_decision.reason,
            },
        )
        accepts_fact_candidates = (
            not self.interpreter or controller_decision.accepts_fact_candidates
        )
        expected_date_slot = (
            "check_out"
            if conversation.booking_check_in and not conversation.booking_check_out
            else None
        )
        booking_update = (
            self._booking_update_from_interpretation(
                interpretation,
                caller_text=text,
                expected_date_slot=expected_date_slot,
            )
            if self.interpreter and accepts_fact_candidates
            else (
                self._extract_booking_slot_update(
                    text,
                    today=date.today(),
                    expected_date_slot=expected_date_slot,
                )
                if not self.interpreter and accepts_fact_candidates
                else BookingSlotUpdate()
            )
        )
        await self._record_audit(
            conversation.id,
            "booking_slots_extracted",
            {
                "guest_text": text,
                "slots": {
                    "check_in": (
                        booking_update.check_in.isoformat()
                        if booking_update.check_in
                        else None
                    ),
                    "check_out": (
                        booking_update.check_out.isoformat()
                        if booking_update.check_out
                        else None
                    ),
                    "guest_count": booking_update.guest_count,
                    "nights": booking_update.nights,
                    "room_preference": (
                        interpretation.booking_room_preference if interpretation else None
                    ),
                    "booking_plan_json": (
                        interpretation.booking_plan_json if interpretation else None
                    ),
                },
            },
        )
        booking_turn = bool(
            booking_update.has_value
            or conversation.pending_category == "booking"
            or (interpretation and interpretation.intent == "booking")
        )
        fact_proposals: list[FactProposal] = []
        if booking_turn:
            selected_room = bool(
                interpretation
                and interpretation.booking_room_preference
                and dialogue_decision.caller_selected_option
                and (not interpretation.read_only_tools or interpretation.explicit_selection)
            )
            selected_plan = bool(
                interpretation
                and interpretation.booking_plan_json
                and dialogue_decision.can_update_booking_plan
                and not self._is_initial_party_composition_update(
                    conversation,
                    interpretation,
                )
            )
            booking_update, fact_proposals = self._stage_booking_fact_changes(
                conversation,
                booking_update,
                caller_text=text,
                source_turn_id=turn_id,
                interpretation=interpretation,
                selected_room=selected_room,
                selected_plan=selected_plan,
            )
        interpreted_criteria_changed = False
        if (
            interpretation
            and interpretation.intent == "booking"
            and accepts_fact_candidates
        ):
            catalog = await CatalogSnapshotService(
                self.session, self.hotel_id, telemetry=self.telemetry
            ).get()
            interpreted_criteria_changed = await self._apply_booking_criteria_patch(
                conversation,
                {
                    "required_features": interpretation.booking_required_features,
                    "preferred_features": interpretation.booking_preferred_features,
                    "excluded_features": interpretation.booking_excluded_features,
                    "removed_features": interpretation.booking_removed_features,
                    "max_nightly_rate_cents": interpretation.booking_max_nightly_rate_cents,
                    "clear_max_nightly_rate": interpretation.booking_clear_max_nightly_rate,
                    "bed_type": interpretation.booking_bed_type,
                    "accessibility_required": interpretation.booking_accessibility_required,
                    "criteria_evidence": interpretation.booking_criteria_evidence,
                },
                caller_text=text,
                catalog=catalog,
                source_turn_id=turn_id,
            )
        ledger_after_criteria = self._booking_fact_ledger(conversation)
        for field in ledger_after_criteria.pending_fields():
            if any(proposal.field == field for proposal in fact_proposals):
                continue
            pending = ledger_after_criteria.pending(field)
            current = ledger_after_criteria.active(field)
            if pending:
                fact_proposals.append(
                    FactProposal(
                        field,
                        "pending_confirmation",
                        current_value=current.value if current else None,
                        proposed_value=pending.value,
                        pending_value=pending.value,
                    )
                )
        plan_before_turn = BookingPlan.from_json(conversation.booking_plan_json)
        interpreted_plan_changed = await self._apply_interpreted_booking_plan(
            conversation,
            interpretation,
            caller_text=text,
        ) if accepts_fact_candidates and not any(
            proposal.field == "booking_plan"
            and proposal.outcome in {"pending_confirmation", "ambiguous"}
            for proposal in fact_proposals
        ) else False
        plan_after_turn = BookingPlan.from_json(conversation.booking_plan_json)
        if interpreted_plan_changed or plan_after_turn.has_exact_allocation:
            # The domain reducer has now separated caller-owned layout facts
            # from the interpreter's proposal. Reassess the turn with that
            # structured evidence before deciding whether a composition
            # question is still necessary.
            dialogue_decision = self.dialogue_controller.assess_turn(
                interpretation,
                has_active_offer=bool(conversation.booking_offer_id),
                has_search_evidence=bool(conversation.booking_last_search_result),
                caller_has_explicit_layout=(
                    plan_after_turn.has_exact_allocation
                    or plan_after_turn.requested_room_count is not None
                    or plan_after_turn.allow_mixed_types
                ),
            )
        party_composition_changed = (
            interpreted_plan_changed
            and plan_after_turn.party_composition_known
            and (
                not plan_before_turn.party_composition_known
                or plan_before_turn.party_groups != plan_after_turn.party_groups
                or plan_before_turn.allow_room_sharing
                != plan_after_turn.allow_room_sharing
            )
        )
        pending_fact_prompt = self._booking_fact_confirmation_prompt(fact_proposals)
        if pending_fact_prompt:
            conversation.pending_category = "booking"
            conversation.active_topic = "booking"
            await self.store.add_turn(conversation.id, "assistant", pending_fact_prompt)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "booking", pending_fact_prompt)
        if (
            party_composition_changed
            and conversation.booking_check_in
            and conversation.booking_check_out
            and conversation.booking_guest_count
        ):
            # Composition is caller-owned state, so the next recommendation is
            # a fresh PMS lookup using that state. This is one lookup and no
            # second summary-model call.
            composition_response = await self._refresh_booking_options_for_comparison(
                conversation,
                on_response=on_response,
            )
            if composition_response:
                return composition_response
        if interpreted_plan_changed and not self._booking_lookup_completed_this_turn:
            # A caller-owned layout fact must advance the slot-filling flow in
            # the same turn. Otherwise the model can acknowledge the fact and
            # leave the call with no next question. This is local state work:
            # no second LLM call and no PMS lookup are needed.
            progression_prompt = self.booking_coordinator.progressive_booking_prompt(
                conversation
            )
            if progression_prompt:
                conversation.pending_category = "booking"
                conversation.active_topic = "booking"
                conversation.state = "collecting_booking_allocation"
                await self.store.add_turn(
                    conversation.id, "assistant", progression_prompt
                )
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "booking", progression_prompt)
        if (
            not interpreted_plan_changed
            and interpretation
            and interpretation.intent == "booking"
            and interpretation.booking_plan_json
            and plan_after_turn.party_composition_known
            and not self._booking_lookup_completed_this_turn
        ):
            # Repeating an already-known party answer is an idempotent semantic
            # turn, not a correction and not an interpreter failure. Continue
            # from the persisted booking state instead of asking the caller to
            # repeat themselves.
            progression_prompt = self.booking_coordinator.progressive_booking_prompt(
                conversation
            )
            if progression_prompt:
                conversation.pending_category = "booking"
                conversation.active_topic = "booking"
                conversation.state = "collecting_booking_allocation"
                await self.store.add_turn(
                    conversation.id, "assistant", progression_prompt
                )
                await self.session.commit()
                return HotelTurnResponse(
                    conversation.id, "booking", progression_prompt
                )
        if (
            interpretation
            and interpretation.intent == "booking"
            and interpretation.dialogue_act not in {"ask", "compare"}
            and plan_after_turn.party_composition_known
            and plan_after_turn.party_groups
            and plan_after_turn.allow_room_sharing is False
            and not plan_after_turn.has_exact_allocation
        ):
            # A described couple is already a non-sharing party group. The
            # remaining guests are flexible for PMS allocation; asking them
            # again whether they share with the couple is redundant.
            if not conversation.booking_check_in and not conversation.booking_check_out:
                say = self.language_adapter.text("booking_party_layout_dates")
            elif not conversation.booking_check_in:
                say = self.language_adapter.text("booking_check_in")
            elif not conversation.booking_check_out:
                say = self.language_adapter.text("booking_check_out")
            else:
                say = None
            if say:
                conversation.pending_category = "booking"
                conversation.active_topic = "booking"
                conversation.state = "collecting_booking_allocation"
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "booking", say)
        if (
            booking_turn
            and booking_update.has_value
            and conversation.booking_guest_count
            and conversation.booking_guest_count > 1
            and not plan_after_turn.party_composition_known
            and not plan_after_turn.has_exact_allocation
        ):
            if (
                conversation.booking_check_in
                and conversation.booking_nights
                and not conversation.booking_check_out
            ):
                conversation.booking_check_out = conversation.booking_check_in + timedelta(
                    days=conversation.booking_nights
                )
            say = self.language_adapter.text(
                "booking_party_composition",
                guest_count=conversation.booking_guest_count,
            )
            conversation.pending_category = "booking"
            conversation.active_topic = "booking"
            conversation.state = "collecting_booking_allocation"
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "booking", say)
        if self._controller_owns_tools and controller_decision.command in {
            "ask_about_state",
            "request_recommendation",
        }:
            # Re-evaluate after the reducer above has accepted this turn's
            # caller facts.  The first controller snapshot is intentionally
            # pre-reduction because it must protect confirmation handling.
            post_reduction_decision = self.dialogue_controller.decide_turn(
                DialogueSnapshot(
                    state=conversation.state,
                    pending_category=conversation.pending_category,
                    has_active_offer=bool(conversation.booking_offer_id),
                    has_search_evidence=bool(conversation.booking_last_search_result),
                    has_booking_dates=bool(
                        conversation.booking_check_in and conversation.booking_check_out
                    ),
                    has_guest_count=bool(conversation.booking_guest_count),
                    has_room_preference=bool(conversation.booking_room_preference),
                    has_pending_fact_confirmation=self._booking_fact_ledger(
                        conversation
                    ).has_pending(),
                ),
                interpretation,
            )
            await self._record_audit(
                conversation.id,
                "controller_post_reduction_decision",
                {
                    "command": post_reduction_decision.command,
                    "action": post_reduction_decision.action,
                    "reason": post_reduction_decision.reason,
                    "requires_tool": post_reduction_decision.requires_tool,
                },
            )
            if (
                post_reduction_decision.action == "answer_from_state"
                and post_reduction_decision.requires_tool
            ):
                state_response = await self._run_controller_state_lookup(
                    conversation,
                    post_reduction_decision,
                )
                if state_response:
                    return state_response
            if post_reduction_decision.action == "request_recommendation":
                recommendation_response = await self._run_controller_recommendation(
                    conversation,
                    on_response=on_response,
                )
                if recommendation_response:
                    return recommendation_response
        if dialogue_decision.should_refresh_availability:
            comparison_response = await self._refresh_booking_options_for_comparison(
                conversation,
                on_response=on_response,
            )
            if comparison_response:
                return comparison_response
        if (
            interpreted_criteria_changed
            and conversation.booking_check_in
            and conversation.booking_check_out
            and conversation.booking_guest_count
        ):
            recommendation_response = await self._refresh_booking_options_for_comparison(
                conversation,
                on_response=on_response,
            )
            if recommendation_response:
                return recommendation_response

        # A suspended workflow never receives a bare confirmation meant for
        # the task currently in front of the caller. It can only resume after
        # this explicit, fresh resume prompt.
        if conversation.state == "awaiting_deferred_resume":
            if self._is_affirmative(normalized):
                category = self._resume_deferred_workflow(conversation)
                assert category is not None
                say = self._deferred_resume_prompt(category)
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "workflow_resumed", say)
            if self._is_negative(normalized):
                self._clear_deferred_workflow(conversation)
                conversation.state = "ready"
                say = "No problem. I won't continue the earlier request."
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "workflow_cancelled", say)
        elif conversation.deferred_category:
            conversation.deferred_turns += 1
            if conversation.deferred_turns > 4:
                logger.info(
                    "Expiring deferred hotel workflow category={} after {} caller turns",
                    conversation.deferred_category,
                    conversation.deferred_turns,
                )
                self._clear_deferred_workflow(conversation)

        captured_name, captured_room = (
            (interpretation.name, interpretation.room_number)
            if interpretation
            else self._extract_guest_context(text)
        )
        request_rule = (
            self._request_rule_from_interpretation(interpretation)
            if self.interpreter
            else self._match_request(normalized)
        )
        # "recommend" is also a concierge keyword, but inside an active
        # reservation flow it is a request to continue the booking plan.  A
        # hotel-service route must never steal dates or party allocation.
        if conversation.pending_category == "booking":
            if self.interpreter:
                if interpretation and interpretation.intent == "booking":
                    request_rule = None
            elif booking_update.has_value or re.search(
                r"\b(recommend|recommendation|suggest|best option)\b", normalized
            ):
                request_rule = None
        if (
            request_rule
            and conversation.state == "awaiting_deferred_resume"
            and conversation.deferred_category
        ):
            say = (
                f"I still have your {self._workflow_label(conversation.deferred_category)} on hold. "
                "Please tell me whether to continue or discard it before starting another request."
            )
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "clarification", say)
        if request_rule and conversation.pending_category:
            incoming_category = request_rule[2]
            switch = self._suspend_active_workflow(
                conversation, incoming_category=incoming_category
            )
            if switch == "full":
                say = (
                    f"I'm already holding your {self._workflow_label(conversation.deferred_category)} "
                    f"while we handle your {self._workflow_label(conversation.pending_category)}. "
                    "Let's finish or cancel one of those before starting another request."
                )
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "clarification", say)
            if switch == "suspended":
                conversation.pending_category = incoming_category
                conversation.pending_description = text.strip()
                conversation.state = (
                    "awaiting_room_service_details"
                    if incoming_category == "room_service"
                    else "awaiting_issue_details"
                )
                follow_up = "What details should I include?"
                say = (
                    f"I'll hold your {self._workflow_label(conversation.deferred_category)} while we handle this. "
                    f"{follow_up}"
                )
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "clarification", say)
        previous_name = conversation.caller_name
        previous_room = conversation.room_number
        previous_state = conversation.state
        name_was_updated = bool(
            captured_name
            and (not previous_name or captured_name.casefold() != previous_name.casefold())
        )
        room_was_updated = bool(captured_room and captured_room != previous_room)
        name_completes_booking = bool(
            name_was_updated
            and previous_state == "awaiting_booking_details"
            and conversation.pending_category == "booking"
        )
        # A room-number answer can complete the clarification for an issue
        # already captured on the previous turn. Preserve that issue instead
        # of sending the caller back to a generic "describe it" prompt.
        if (
            request_rule is None
            and conversation.pending_category in {"maintenance", "housekeeping", "security"}
            and conversation.pending_description
            and conversation.room_number
            and room_was_updated
        ):
            request_rule = next(
                (rule for rule in self._request_rules if rule[2] == conversation.pending_category),
                None,
            )
        if captured_name or captured_room:
            await self.store.update_guest_context(
                conversation,
                full_name=captured_name,
                room_number=captured_room,
            )
        if conversation.caller_name and (conversation.room_number or conversation.reservation_id):
            conversation.verification_status = "caller_provided"

        if (
            (previous_name and name_was_updated) or (previous_room and room_was_updated)
        ) and not (
            conversation.pending_category == "booking"
            and conversation.state == "awaiting_booking_details"
        ):
            self._remember_turn_state(
                conversation,
                intent="identity_correction",
                topic="identity",
            )
            if room_was_updated and not name_was_updated:
                say = f"Thanks. You are now in room {conversation.room_number}."
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "identity", say)
            updates: list[str] = []
            if name_was_updated:
                updates.append(f"your name to {conversation.caller_name}")
            if room_was_updated:
                updates.append(f"your room to {conversation.room_number}")
            say = f"I've updated {' and '.join(updates)}."
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "identity", say)

        # Supplying the guest name is the final caller-owned value after an
        # accepted hold.  The interpreter may repeat the persisted dates,
        # guests, or nights in its JSON on this turn; those are context, not a
        # new slot request.  Confirm before the normal booking-slot router can
        # mistake that repetition for a request to reopen the plan.
        if name_completes_booking:
            confirmation = self.booking_flow.confirmation_decision(
                self.booking_flow.lifecycle_snapshot(conversation),
                caller_name_provided=bool(conversation.caller_name),
            )
            if confirmation.action in {"confirm_hold", "hold_missing"}:
                result = await self.booking_coordinator.confirm_current_hold(conversation)
                return self._booking_response(conversation, result)

        if self._asks_for_name(normalized):
            self._remember_turn_state(conversation, intent="identity_question", topic="identity")
            say = (
                f"Your name is {conversation.caller_name}."
                if conversation.caller_name
                else "I do not have your name yet. What name should I use?"
            )
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "identity", say)

        if self._asks_for_room(normalized):
            self._remember_turn_state(conversation, intent="identity_question", topic="identity")
            say = (
                f"Your room number is {conversation.room_number}."
                if conversation.room_number
                else "I do not have your room number yet. Which room are you in?"
            )
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "identity", say)

        # Cancellation must be evaluated before the request keyword matcher.
        # Otherwise "cancel the leak request" is mistaken for a new leak and
        # creates another work order instead of closing the existing one.
        cancellation_requested = (
            (
                bool(interpretation)
                and (
                    interpretation.action == "cancel"
                    or interpretation.intent == "cancellation"
                )
            )
            if self.interpreter
            else self._is_explicit_request_cancellation(normalized)
        )
        booking_cancellation = self.booking_flow.cancellation_decision(
            self.booking_flow.lifecycle_snapshot(conversation),
            requested=cancellation_requested,
        )
        if cancellation_requested and not request_rule:
            if booking_cancellation.action == "cancel_booking":
                result = await self.booking_coordinator.cancel_unfinished_booking(conversation)
                return self._booking_response(conversation, result)
            requests = await self.store.request_history(conversation.id)
            open_requests = [
                request for request in requests if request.status not in {"resolved", "cancelled"}
            ]
            selected = None
            if len(open_requests) == 1:
                selected = open_requests[0]
            elif len(open_requests) > 1:
                # Local matching only resolves a uniquely referenced persisted
                # case. This prevents an ambiguous cancellation from silently
                # targeting the latest request.
                selected = self._match_request_reference(normalized, open_requests)

            if selected:
                selected.status = "cancelled"
                await self.store.apply_request_action(
                    selected,
                    "voice_agent",
                    "cancelled",
                    "Guest cancelled the request during the call.",
                )
                conversation.pending_category = None
                conversation.pending_description = None
                conversation.state = "ready"
                resume_note = self._offer_deferred_resume(conversation)
                self._remember_turn_state(
                    conversation,
                    intent="cancellation",
                    topic="general",
                )
                say = "I cancelled that request." + resume_note
            elif len(open_requests) > 1:
                say = "Which request would you like to cancel?"
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "clarification", say)
            else:
                say = "I couldn't find an open request to cancel."
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "cancelled", say)

        # Start the room-service slot-filling flow before the generic identity
        # guard. This preserves the guest's request while we collect the room
        # number, instead of losing it and asking them to repeat themselves.
        topic = (
            (request_rule[2] if request_rule else None)
            or conversation.pending_category
            or "general"
        )
        intent = (
            interpretation.intent
            if self.interpreter and interpretation
            else request_rule[2] if request_rule else "general"
        )
        self._remember_turn_state(
            conversation,
            intent=intent,
            topic=topic,
        )
        # A new maintenance/security/housekeeping issue interrupts a pending
        # food order. Never let the old room-service slot swallow the new
        # request (for example: "cancel the order; the bathroom pipe is leaking").
        if conversation.pending_category == "room_service" and request_rule:
            if request_rule[2] != "room_service":
                conversation.pending_category = None
                conversation.pending_description = None
                conversation.state = "ready"
        if request_rule and request_rule[2] == "room_service":
            if conversation.pending_category != "room_service":
                conversation.pending_category = "room_service"
                # Let the menu lookup below decide whether the utterance names
                # a configured item.
                conversation.pending_description = None
                conversation.state = "awaiting_room_service_details"

        # A room is required to route a hotel request; a name is useful context
        # but should never block ordinary conversation or service intake. This
        # prevents an unanswered name question from trapping the caller in a
        # repeated identity loop.
        first_issue_intake = bool(
            request_rule
            and request_rule[2] in {"maintenance", "housekeeping", "security"}
            and conversation.pending_category != request_rule[2]
            and (
                not self.interpreter
                or not interpretation
                or interpretation.action not in {"create_request", "confirm"}
            )
        )
        action_requires_room = (
            bool(
                (request_rule or conversation.pending_category)
                and conversation.pending_category != "booking"
            )
            and not first_issue_intake
        )
        if action_requires_room and not (conversation.room_number or conversation.reservation_id):
            say = "I need your room number before I can send that request. Which room are you in?"
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "identity", say)

        # If the caller supplied the room after asking for room service, do not
        # interpret the room number as the food order itself.
        if (
            conversation.pending_category == "room_service"
            and room_was_updated
            and not request_rule
        ):
            conversation.state = "awaiting_room_service_details"
            say = "Thanks. What would you like to order?"
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "clarification", say)

        if conversation.pending_category == "room_service":
            response = await self._handle_pending_room_service(conversation, text, normalized)
            await self.session.commit()
            return response

        if request_rule:
            department_key, priority, category, _ = request_rule
            if (
                self.interpreter
                and interpretation
                and interpretation.action in {"ask_slot", "clarify"}
            ):
                conversation.pending_category = category
                conversation.pending_description = (
                    interpretation.service_description or text.strip()
                )
                conversation.state = (
                    "awaiting_room_service_details"
                    if category == "room_service"
                    else "awaiting_issue_details"
                )
                follow_up = (
                    interpretation.follow_up_question
                    or interpretation.response
                    or "What details should I include?"
                )
                await self.store.add_turn(conversation.id, "assistant", follow_up)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "clarification", follow_up)
            # First mention is intake only.  Require one follow-up turn before
            # creating a maintenance/housekeeping/security request so a noisy
            # or unfinished utterance cannot dispatch work immediately.
            if (
                not self.interpreter
                and category in {"maintenance", "housekeeping", "security"}
                and not (category == "maintenance" and re.search(r"\bleak(?:ing)?\b", normalized))
            ):
                if not (
                    conversation.pending_category == category and conversation.pending_description
                ):
                    conversation.pending_category = category
                    conversation.pending_description = text.strip()
                    conversation.state = "awaiting_issue_details"
                    follow_up = (
                        self._interpretation_response(interpretation)
                        or (interpretation.follow_up_question if interpretation else None)
                        or (
                            "What would you like to order?"
                            if category == "room_service"
                            else "Could you tell me a little more about the issue?"
                        )
                    )
                    await self.store.add_turn(conversation.id, "assistant", follow_up)
                    await self.session.commit()
                    return HotelTurnResponse(conversation.id, "clarification", follow_up)
            self._remember_turn_state(
                conversation,
                intent="request_created",
                topic=category,
            )
            department = await self.store.department(self.hotel_id, department_key)
            request = await self.store.create_request(
                conversation=conversation,
                category=category,
                description=(
                    f"{conversation.pending_description}; follow-up: "
                    f"{interpretation.service_description if interpretation and interpretation.service_description else text}"
                    if conversation.pending_description
                    else interpretation.service_description
                    if interpretation and interpretation.service_description
                    else text
                ),
                priority=priority,
                room_number=conversation.room_number,
                department_id=department.id if department else None,
            )
            request.notification_status = "queued"
            if self.messaging and department and department.escalation_phone:
                message = (
                    f"Evergreen Harbor request {str(request.id)[:8]}: {category} for room "
                    f"{conversation.room_number or 'unknown'}. Reply to the front desk if you need context."
                )
                try:
                    reference = await self._notify(
                        department.escalation_phone,
                        message,
                        idempotency_key=f"hotel-request:{request.id}",
                        aggregate_type="hotel_request",
                        aggregate_id=request.id,
                    )
                    request.notification_status = (
                        "queued" if reference.startswith("outbox:") else "sent"
                    )
                except Exception as exc:  # notification failure must not lose the request
                    request.notification_status = "failed"
                    await self.store.apply_request_action(
                        request,
                        "system",
                        "notification_failed",
                        str(exc),
                    )
            conversation.pending_category = None
            conversation.pending_description = None
            conversation.state = "ready"
            resume_note = self._offer_deferred_resume(conversation)
            await self.session.commit()
            if priority == "emergency":
                say = "I’ve escalated this safety concern to the front desk immediately."
            else:
                say = f"I’ve logged your {category} request for room {conversation.room_number}."
            return HotelTurnResponse(
                conversation.id, "request_created", say + resume_note, request.id
            )

        # Booking values take precedence over FAQ matching.  Without this
        # boundary, "my check-in is tomorrow" is misread as a question about
        # the property's check-in *time*, and the caller's date is discarded.
        # Do this before generic facts or model prose can route the turn away
        # from the active booking.
        booking_intent = (
            interpretation is not None and interpretation.intent == "booking"
            if self.interpreter
            else self._looks_like_room_inquiry(normalized)
        )
        booking_transition = self.booking_flow.transition(
            BookingStateSnapshot(
                check_in=conversation.booking_check_in,
                check_out=conversation.booking_check_out,
                guest_count=conversation.booking_guest_count,
                pending_category=conversation.pending_category,
                pending_description=conversation.pending_description,
                has_offer=bool(conversation.booking_offer_id),
                has_hold=bool(conversation.booking_hold_reservation_id),
                nights=conversation.booking_nights,
            ),
            booking_update,
            booking_intent=booking_intent,
            caller_text=text,
            lookup_completed_this_turn=self._booking_lookup_completed_this_turn,
        )
        if booking_transition.active:
            if booking_transition.invalid_dates:
                say = self.language_adapter.text("invalid_dates")
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "clarification", say)
            conversation.booking_check_in = booking_transition.next_check_in
            conversation.booking_check_out = booking_transition.next_check_out
            conversation.booking_guest_count = booking_transition.next_guest_count
            conversation.booking_nights = booking_transition.next_nights
            if booking_transition.invalidate_offer:
                if conversation.booking_hold_reservation_id:
                    await HotelPMSAdapter(self.session, self.hotel_id).cancel_booking_hold(
                        conversation.booking_hold_reservation_id
                    )
                    conversation.booking_hold_reservation_id = None
                await HotelBookingOfferService(
                    self.session, self.hotel_id, telemetry=self.telemetry
                ).invalidate_presented_offers(
                    conversation,
                    reason="The caller changed booking dates or party size.",
                    commit=False,
                )
                # A lookup completed in this very model turn was made with
                # the newly stated booking values. Keep that evidence; only a
                # prior-turn lookup becomes stale here.
                if booking_transition.clear_previous_search:
                    conversation.booking_last_search_result = None
            conversation.pending_category = booking_transition.next_pending_category
            conversation.pending_description = booking_transition.next_pending_description
            self._remember_turn_state(conversation, intent="booking", topic="booking")
            if self.interpreter and interpretation:
                if interpreted_plan_changed:
                    offer_response = await self.booking_coordinator.present_current_offer(
                        conversation
                    )
                    if offer_response:
                        await self.store.add_turn(conversation.id, "assistant", offer_response)
                        await self.session.commit()
                        return HotelTurnResponse(conversation.id, "booking", offer_response)
                # The semantic parser owns only meaning and facts. It is
                # intentionally allowed to omit caller-facing prose, so a
                # valid date/guest update must advance from persisted state
                # instead of falling through to the generic repeat response.
                # This is local policy work: it adds neither an LLM call nor
                # a PMS query.
                if not self._booking_lookup_completed_this_turn and not interpretation.response:
                    progression_prompt = self.booking_coordinator.progressive_booking_prompt(
                        conversation
                    )
                    if progression_prompt:
                        conversation.state = "collecting_booking_allocation"
                        await self.store.add_turn(
                            conversation.id, "assistant", progression_prompt
                        )
                        await self.session.commit()
                        return HotelTurnResponse(
                            conversation.id, "booking", progression_prompt
                        )
                if interpretation.action in {
                    "answer",
                    "ask_slot",
                    "clarify",
                    "backchannel",
                    "none",
                }:
                    model_response = await self._return_interpreter_response(
                        conversation, interpretation
                    )
                    if model_response:
                        return model_response
                return await self._return_interpreter_failure(conversation)
            say = await self.booking_coordinator.fallback_response(conversation, text)
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "booking", say)

        # Name capture happens before this point. A reservation is confirmed
        # only after the caller accepted a dated hold and supplied a name;
        # asking for a quote never creates a confirmed stay.
        confirmation = self.booking_flow.confirmation_decision(
            self.booking_flow.lifecycle_snapshot(conversation),
            caller_name_provided=bool(conversation.caller_name),
        )
        if confirmation.action in {"confirm_hold", "hold_missing"}:
            result = await self.booking_coordinator.confirm_current_hold(conversation)
            return self._booking_response(conversation, result)

        if self.interpreter and interpreted_plan_changed:
            offer_response = await self.booking_coordinator.present_current_offer(conversation)
            if offer_response:
                self._remember_turn_state(conversation, intent="booking", topic="booking")
                await self.store.add_turn(conversation.id, "assistant", offer_response)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "booking", offer_response)

        if self.interpreter and interpretation:
            allowed_response = interpretation.action in {"answer", "backchannel", "none"}
            allowed_response = allowed_response or (
                interpretation.intent == "booking"
                and interpretation.action in {"ask_slot", "clarify"}
            )
            if allowed_response:
                model_response = await self._return_interpreter_response(
                    conversation, interpretation
                )
                if model_response:
                    return model_response

            if interpretation.intent == "booking" and interpretation.action not in {
                "confirm",
                "cancel",
            }:
                return await self._return_interpreter_failure(conversation)

        facts = await self.store.active_facts(self.hotel_id)
        answer = self._find_fact(normalized, facts)
        if answer:
            self._remember_turn_state(
                conversation,
                intent="faq",
                topic="faq",
            )
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "faq", answer)

        if captured_name or captured_room:
            if captured_name and not conversation.room_number:
                say = (
                    f"Thanks, {conversation.caller_name}. I still need your room number before I "
                    "can route a request. How can I help?"
                )
            elif captured_room and not captured_name and conversation.caller_name:
                say = f"Thanks, {conversation.caller_name} in room {conversation.room_number}. How can I help?"
            else:
                say = (
                    f"Thanks, {conversation.caller_name or 'I have your details'} in room "
                    f"{conversation.room_number or 'number pending'}. How can I help?"
                )
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "identity", say)

        # Keep a booking conversation inside its slot-filling state. Short
        # follow-ups such as "three guests" or "check tomorrow" do not carry
        # the word "room", but they still belong to the active booking.
        if not self.interpreter:
            acceptance = self.booking_flow.acceptance_decision(
                self.booking_flow.lifecycle_snapshot(conversation),
                affirmative=self._is_affirmative(normalized),
            )
            if acceptance.action == "accept_offer":
                result = await self.booking_coordinator.accept_current_offer(conversation)
                return self._booking_response(conversation, result)

        if not self.interpreter and conversation.pending_category == "booking":
            if re.search(r"\b(other|another|alternative|options?)\b", text.casefold()):
                say = await self.booking_coordinator.options_response(conversation, text)
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "booking_options", say)
            if conversation.booking_guest_count and re.search(
                r"\b(recommend|recommendation|suggest|best option)\b", normalized
            ):
                # Capacity is a PMS fact, not a model judgement. In
                # particular, never recommend a four-person suite as the
                # complete answer to a five-person booking.
                say = await self.booking_coordinator.fallback_response(conversation, text)
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, "booking_options", say)
            interruption = await self.booking_coordinator.information_response(
                conversation, text, normalized
            )
            if interruption:
                response_intent, say = interruption
                await self.store.add_turn(conversation.id, "assistant", say)
                await self.session.commit()
                return HotelTurnResponse(conversation.id, response_intent, say)
            # This is the explicitly legacy, no-interpreter path. Its
            # coordinator performs the domain parsing and validation; the
            # production path above never reaches it.
            say = await self.booking_coordinator.fallback_response(conversation, text)
            conversation.pending_description = self._append_booking_context(
                conversation.pending_description, text
            )
            self._remember_turn_state(conversation, intent="booking", topic="booking")
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "booking", say)
        if not self.interpreter and booking_intent:
            say = await self.booking_coordinator.fallback_response(conversation, text)
            self._remember_turn_state(conversation, intent="booking", topic="booking")
            conversation.pending_category = "booking"
            conversation.pending_description = self._append_booking_context(
                conversation.pending_description, text
            )
            await self.store.add_turn(conversation.id, "assistant", say)
            await self.session.commit()
            return HotelTurnResponse(conversation.id, "booking", say)

        say = self.language_adapter.text("general")
        self._remember_turn_state(conversation, intent="general", topic="general")
        await self.session.commit()
        return HotelTurnResponse(conversation.id, "conversation", say)

    @staticmethod
    def _match_request_reference(normalized: str, requests: list[HotelRequestRecord]):
        stop_words = {
            "cancel",
            "cancellation",
            "the",
            "request",
            "case",
            "please",
            "would",
            "like",
            "to",
            "close",
            "that",
            "one",
            "just",
            "made",
        }
        words = {
            word
            for word in re.findall(r"[a-z0-9]+", normalized)
            if word not in stop_words and len(word) > 2
        }
        if not words:
            return None
        matches = []
        for request in requests:
            haystack = " ".join(
                (request.category, request.description, request.room_number or "")
            ).lower()
            if any(word in haystack for word in words):
                matches.append(request)
        return matches[0] if len(matches) == 1 else None

    async def route_voice_turn(
        self,
        conversation_id: UUID,
        text: str,
        *,
        on_response: Callable[[str], Awaitable[None]] | None = None,
    ) -> HotelVoiceRoute:
        # The hotel workflow owns one semantic model call and returns one
        # response for every turn. It never falls through to a second general
        # purpose LLM in the Pipecat pipeline.
        response = await self.handle_turn(conversation_id, text, on_response=on_response)
        return HotelVoiceRoute(response=response)

    async def record_assistant_turn(self, conversation_id: UUID, text: str) -> None:
        await self.store.add_turn(conversation_id, "assistant", text)
        await self.session.commit()

    async def latest_request(self, conversation_id: UUID) -> HotelRequestRecord | None:
        return await self.store.latest_request(conversation_id)

    async def _create_request_response(
        self,
        *,
        conversation,
        department_key: str,
        priority: str,
        category: str,
        description: str,
    ) -> HotelTurnResponse:
        department = await self.store.department(self.hotel_id, department_key)
        request = await self.store.create_request(
            conversation=conversation,
            category=category,
            description=description,
            priority=priority,
            room_number=conversation.room_number,
            department_id=department.id if department else None,
        )
        self._remember_turn_state(conversation, intent="request_created", topic=category)
        request.notification_status = "queued"
        if self.messaging and department and department.escalation_phone:
            message = (
                f"Evergreen Harbor request {str(request.id)[:8]}: {category} for room "
                f"{conversation.room_number or 'unknown'}. Reply to the front desk if you need context."
            )
            try:
                reference = await self._notify(
                    department.escalation_phone,
                    message,
                    idempotency_key=f"hotel-request:{request.id}",
                    aggregate_type="hotel_request",
                    aggregate_id=request.id,
                )
                request.notification_status = (
                    "queued" if reference.startswith("outbox:") else "sent"
                )
            except Exception as exc:  # notification failure must not lose the request
                request.notification_status = "failed"
                await self.store.apply_request_action(
                    request,
                    "system",
                    "notification_failed",
                    str(exc),
                )
        resume_note = self._offer_deferred_resume(conversation)
        await self.session.commit()
        if priority == "emergency":
            say = "I’ve escalated this safety concern to the front desk immediately."
        else:
            say = f"I’ve logged your {category} request for room {conversation.room_number}."
        # The commit above is the success boundary for the voice workflow.
        # Acknowledge it before the concrete outcome so the guest hears that
        # the operation completed, not just a generated-sounding sentence.
        # The action has committed successfully. Use a compact confirmation
        # with a small pause so the completion sounds earned, not announced
        # before the database/notification work is actually done.
        say = self._success_acknowledgement(say) + resume_note
        return HotelTurnResponse(conversation.id, "request_created", say, request.id)

    async def _notify(
        self,
        phone: str,
        message: str,
        *,
        idempotency_key: str,
        aggregate_type: str,
        aggregate_id: UUID | None,
    ) -> str:
        if self.notification_queue:
            return await self.notification_queue.enqueue(
                channel=self.notification_channel,
                phone=phone,
                message=message,
                idempotency_key=idempotency_key,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
            )
        if not self.messaging:
            raise RuntimeError("No messaging adapter configured")
        if self.notification_channel == "whatsapp":
            return await self.messaging.send_whatsapp(
                phone, message, idempotency_key=idempotency_key
            )
        return await self.messaging.send_sms(phone, message, idempotency_key=idempotency_key)

    async def _handle_pending_room_service(
        self,
        conversation,
        text: str,
        normalized: str,
    ) -> HotelTurnResponse:
        """Collect and confirm a food order before creating a request."""

        if not conversation.room_number and not conversation.reservation_id:
            return HotelTurnResponse(
                conversation.id,
                "identity",
                "What room should I send the order to?",
            )

        if conversation.state == "awaiting_room_service_confirmation":
            if self._is_negative(normalized):
                conversation.pending_category = None
                conversation.pending_description = None
                conversation.state = "ready"
                resume_note = self._offer_deferred_resume(conversation)
                return HotelTurnResponse(
                    conversation.id,
                    "cancelled",
                    "No problem. I did not send a room-service request." + resume_note,
                )
            if self._is_menu_question(normalized):
                conversation.state = "awaiting_room_service_details"
                conversation.pending_description = None
                return await self._menu_listing_response(conversation)
            if False and self._is_affirmative(normalized):
                description = conversation.pending_description or "Room-service order"
                conversation.pending_category = None
                conversation.pending_description = None
                conversation.state = "ready"
                # The caller confirmed the order; the normal request path will
                # create and notify Food and Beverage below on the next turn.
                conversation.state = "ready"
                return HotelTurnResponse(
                    conversation.id,
                    "room_service_confirmed",
                    f"I’m sending your room-service request for {description} now.",
                )
            if self._is_affirmative(normalized):
                description = conversation.pending_description or "Room-service order"
                conversation.pending_category = None
                conversation.pending_description = None
                conversation.state = "ready"
                return await self._create_request_response(
                    conversation=conversation,
                    department_key="food_beverage",
                    priority="routine",
                    category="room_service",
                    description=description,
                )
            menu_response = await self._menu_confirmation_response(conversation, text)
            if menu_response:
                return menu_response
            if self._is_generic_room_service_request(normalized):
                conversation.pending_description = None
                conversation.state = "awaiting_room_service_details"
                return HotelTurnResponse(
                    conversation.id,
                    "clarification",
                    "What would you like to order?",
                )
            if self._looks_like_room_service_details(normalized):
                return await self._unknown_menu_item_response(conversation)
            return HotelTurnResponse(
                conversation.id,
                "confirmation",
                "Please say yes to send that order, or no to cancel it.",
            )

        # We are waiting for the actual food or drink details.
        if self._is_negative(normalized):
            conversation.pending_category = None
            conversation.pending_description = None
            conversation.state = "ready"
            resume_note = self._offer_deferred_resume(conversation)
            return HotelTurnResponse(
                conversation.id,
                "cancelled",
                "No problem. I did not send a room-service request." + resume_note,
            )
        if self._is_menu_question(normalized):
            conversation.state = "awaiting_room_service_details"
            conversation.pending_description = None
            return await self._menu_listing_response(conversation)
        if self._is_generic_room_service_request(normalized):
            return HotelTurnResponse(
                conversation.id,
                "clarification",
                "What would you like to order?",
            )
        menu_response = await self._menu_confirmation_response(conversation, text)
        if menu_response:
            return menu_response
        if self._looks_like_room_service_details(normalized):
            return await self._unknown_menu_item_response(conversation)
        return HotelTurnResponse(
            conversation.id,
            "clarification",
            "What food or drink would you like delivered to your room?",
        )

    @staticmethod
    def _room_service_confirmation_prompt(conversation, price_note: str = "") -> str:
        description = conversation.pending_description or "that order"
        return (
            f"I found {description} on the current menu{price_note}. "
            f"I can send it to room {conversation.room_number}. "
            "Would you like me to send it to the kitchen?"
        )

    async def _menu_confirmation_response(
        self, conversation, text: str
    ) -> HotelTurnResponse | None:
        matches = await self.store.menu_matches(self.hotel_id, text)
        if not matches:
            return None
        # A single alias such as "Maggie" is normalized to the hotel’s
        # canonical item. For multiple items, retain the caller’s wording so
        # the request remains faithful to the order they actually gave.
        conversation.pending_description = (
            matches[0].name if len(matches) == 1 else text.strip().strip(".!?")
        )
        conversation.state = "awaiting_room_service_confirmation"
        price_note = ""
        if len(matches) == 1 and matches[0].price_cents is not None:
            price_note = f". The listed price is ${matches[0].price_cents / 100:.2f}"
        return HotelTurnResponse(
            conversation.id,
            "confirmation",
            self._room_service_confirmation_prompt(conversation, price_note),
        )

    async def _unknown_menu_item_response(self, conversation) -> HotelTurnResponse:
        items = await self.store.menu_items(self.hotel_id)
        if items:
            available = ", ".join(item.name for item in items[:6])
            say = (
                "I couldn’t find that item on the current menu. "
                f"Available options include {available}. Which would you like?"
            )
        else:
            say = "The hotel menu is not configured yet. I can connect you with the front desk."
        conversation.state = "awaiting_room_service_details"
        conversation.pending_description = None
        return HotelTurnResponse(conversation.id, "clarification", say)

    async def _menu_listing_response(self, conversation) -> HotelTurnResponse:
        """Answer a menu question without treating the word ``menu`` as an item."""

        items = await self.store.menu_items(self.hotel_id)
        if items:
            available = ", ".join(item.name for item in items[:8])
            say = f"We currently have {available}. What would you like?"
        else:
            say = "The room-service menu is not configured yet. I can connect you with the front desk."
        return HotelTurnResponse(conversation.id, "clarification", say)

    @staticmethod
    def _is_generic_room_service_request(normalized: str) -> bool:
        generic = (
            "food",
            "something to eat",
            "room service",
            "order food",
            "a meal",
            "meal",
            "hungry",
        )
        return any(term in normalized for term in generic) and not any(
            term in normalized
            for term in (
                "noodle",
                "maggie",
                "maggi",
                "manchurian",
                "momo",
                "pizza",
                "burger",
                "sandwich",
                "salad",
                "soup",
                "rice",
                "chicken",
                "pasta",
                "coffee",
                "tea",
                "water",
                "breakfast",
                "lunch",
                "dinner",
                "dessert",
                "fries",
            )
        )

    @staticmethod
    def _is_menu_question(normalized: str) -> bool:
        return any(
            phrase in normalized
            for phrase in (
                "what is on the menu",
                "what's on the menu",
                "what is on menu",
                "what's on menu",
                "what things are on the menu",
                "what do you have",
                "what can i order",
                "show me the menu",
                "menu options",
            )
        ) or normalized.strip() in {"menu", "the menu"}

    @staticmethod
    def _looks_like_room_service_details(normalized: str) -> bool:
        return (
            not any(
                phrase in normalized
                for phrase in (
                    "what is my name",
                    "what's my name",
                    "what is my room",
                    "what's my room",
                    "who am i",
                )
            )
            and len(normalized.strip(" .?!")) >= 3
        )

    @staticmethod
    def _is_affirmative(normalized: str) -> bool:
        cleaned = unicodedata.normalize("NFKC", normalized).casefold()
        cleaned = "".join(
            " " if unicodedata.category(character).startswith("P") else character
            for character in cleaned
        )
        cleaned = " ".join(cleaned.split())
        return cleaned in {
            "ok",
            "okay",
            "ok ji",
            "okay ji",
            "yes",
            "yes please",
            "yeah",
            "yeah please",
            "sure",
            "go ahead",
            "confirm",
            "send it",
            "order it",
            "please do",
            "haan",
            "haan ji",
            "han",
            "han ji",
            "ji",
            "ji haan",
            "theek hai",
            "thik hai",
            "bilkul",
            "sahi",
            "sahi bola",
            "aapne sahi bola",
            "जी",
            "जी हाँ",
            "हाँ",
            "हां",
            "हाँ जी",
            "ठीक है",
            "ठिक है",
            "बिलकुल",
            "सही",
            "सही बोला",
            "आपने सही बोला",
        }

    @classmethod
    def _turn_accepts(
        cls,
        interpretation: HotelTurnInterpretation | None,
        normalized: str,
        *,
        confirmation_context: bool,
    ) -> bool:
        """Prefer semantic approval, with a scoped short-utterance recovery."""

        # Resolve an explicit short polarity in the already-confirming
        # context before trusting a contradictory model label. This keeps a
        # clear "ji" affirmative and "no" negative even when the model
        # returns an incomplete or wrong dialogue act.
        if confirmation_context and cls._is_negative(normalized):
            return False
        if confirmation_context and cls._is_affirmative(normalized):
            return True
        if cls._interpretation_rejects(interpretation):
            return False
        if cls._interpretation_accepts(interpretation):
            return True
        # A one- or two-word acknowledgement is ambiguous in isolation. It
        # is safe to recover it only when the state machine has already asked
        # for confirmation of a concrete fact, offer, or post-action prompt.
        return confirmation_context and cls._is_affirmative(normalized)

    @classmethod
    def _turn_rejects(
        cls,
        interpretation: HotelTurnInterpretation | None,
        normalized: str,
        *,
        confirmation_context: bool,
    ) -> bool:
        """Recognize a negative confirmation without relying on model luck."""

        if not confirmation_context:
            return cls._interpretation_rejects(interpretation)
        if cls._is_affirmative(normalized):
            return False
        if cls._is_negative(normalized):
            return True
        return cls._interpretation_rejects(interpretation)

    @staticmethod
    def _interpretation_accepts(
        interpretation: HotelTurnInterpretation | None,
    ) -> bool:
        """Use the LLM's semantic act for approval, not a phrase dictionary."""

        return bool(
            interpretation
            and (
                interpretation.dialogue_act == "accept"
                or interpretation.action == "confirm"
                or interpretation.intent == "confirmation"
            )
        )

    @staticmethod
    def _interpretation_rejects(
        interpretation: HotelTurnInterpretation | None,
    ) -> bool:
        """Use the LLM's semantic act for rejecting a post-action prompt."""

        return bool(
            interpretation
            and (
                interpretation.dialogue_act in {"reject", "cancel"}
                or interpretation.action == "cancel"
                or interpretation.intent == "cancellation"
            )
        )

    @staticmethod
    def _is_negative(normalized: str) -> bool:
        cleaned = unicodedata.normalize("NFKC", normalized).casefold()
        cleaned = "".join(
            " " if unicodedata.category(character).startswith("P") else character
            for character in cleaned
        )
        cleaned = " ".join(cleaned.split())
        return (
            cleaned
            in {
                "no",
                "no thanks",
                "no ji",
                "nahi",
                "nahi ji",
                "nahin",
                "nahin ji",
                "nah",
                "nah ji",
                "cancel",
                "cancel it",
                "not now",
                "never mind",
                "नहीं",
                "नहीं जी",
                "नही",
                "नही जी",
                "नहीं चाहिए",
                "मत करो",
            }
            or cleaned.startswith("no ")
            or cleaned.startswith("nahi ")
            or cleaned.startswith("nahin ")
            or cleaned.startswith("नहीं ")
            or cleaned.startswith("नही ")
            or cleaned.startswith("cancel ")
        )

    @staticmethod
    def _is_explicit_request_cancellation(normalized: str) -> bool:
        """Recognize cancellation language before service keyword matching."""

        return (
            normalized.startswith(("cancel", "stop", "withdraw"))
            or "cancel that" in normalized
            or "cancel the request" in normalized
            or "no longer need" in normalized
            or "don't need it" in normalized
            or "do not need it" in normalized
        )

    @classmethod
    def _match_request(cls, normalized: str):
        for rule in cls._request_rules:
            if any(phrase in normalized for phrase in rule[3]):
                return rule
        return None

    @staticmethod
    def _extract_guest_context(text: str) -> tuple[str | None, str | None]:
        lowered = text.lower()
        name = None
        room = None
        name_match = re.search(
            # Keep the local extractor conservative.  Bare "I am ..." and
            # "I'm ..." are ordinary conversation ("I'm not hungry", "I'm
            # waiting", "I'm in the bathroom"), not reliable name statements.
            # The one-call planner handles those natural variants when needed.
            r"(?:my\s+name\s+is|my\s+name's|name\s+is|name's|this is|call me)\s+"
            # Do not capture the rejected value in corrections such as
            # "my name is not Vishal; it's Eshaan".
            r"((?!not\b|no\b)[a-z][a-z .'-]{1,80}?)(?=\s+(?:and|my room|room|in room)|[,.!?]|$)",
            text,
            re.I,
        )
        if not name_match and HotelConversationService._is_name_correction(text):
            # Handle natural corrections such as "Not Michelle. It's Ishaan."
            # or "That's not Michelle, I am Ishaan." without treating an
            # arbitrary sentence containing "it's" as an identity update.
            name_match = re.search(
                r"\b(?:it'?s|it is)\s+([a-z][a-z .'-]{1,80}?)(?=\s+(?:and|my room|room|in room)|[,.!?]|$)"
                r"|\b(?:my name is|i am|i'm)\s+(?!not\b|no\b)([a-z][a-z .'-]{1,80}?)(?=\s+(?:and|my room|room|in room)|[,.!?]|$)",
                text,
                re.I,
            )
        if name_match:
            candidate = next(
                (value for value in name_match.groups() if value),
                "",
            )
            candidate = " ".join(candidate.strip().split())
            # Do not mistake location phrases such as "I'm in room 902" for
            # a guest name.  Explicit names remain intentionally conservative.
            if not re.match(r"(?:in|at|from|staying|calling)\b", candidate, re.I):
                name = " ".join(part.capitalize() for part in candidate.split())
        room_match = re.search(
            r"(?:room|suite)(?:\s+number)?\s*(?:is|#)?\s*([a-z0-9 -]{1,32})",
            lowered,
        )
        if room_match:
            room = HotelConversationService._room_digits(room_match.group(1))
        if not room:
            # Guests often answer a direct room-number question with
            # "It's 44" or "the number is four four", without repeating the
            # word "room".
            answer_match = re.search(
                r"(?:it'?s|it is|the number is|number is|that is)\s+"
                r"([a-z0-9 -]{1,32}?)(?=[.!?,]|$)",
                lowered,
            )
            if answer_match:
                room = HotelConversationService._room_digits(answer_match.group(1))
        if not room:
            standalone = re.sub(r"[.!?,]", "", lowered).strip()
            room = HotelConversationService._room_digits(standalone)
        return name, room

    @staticmethod
    def _has_explicit_name_statement(text: str) -> bool:
        normalized = " ".join(text.lower().split())
        return bool(
            re.search(
                r"\b(?:my name is|my name's|name is|name's|i am|i'm|im|this is|call me)\s+[a-z]",
                normalized,
            )
        ) or HotelConversationService._is_name_correction(text)

    @staticmethod
    def _has_explicit_room_statement(text: str) -> bool:
        normalized = " ".join(text.lower().split())
        return bool(
            re.search(r"\b(?:room|suite)(?:\s+number)?\b", normalized)
            or re.search(r"\b(?:it's|it is|the number is|number is)\s+[a-z0-9]", normalized)
        )

    @staticmethod
    def _is_name_correction(text: str) -> bool:
        normalized = " ".join(text.lower().split())
        # A generic occurrence of "not" is not a correction: "I'm not
        # hungry" and "the pipe is not working" are ordinary requests. A
        # correction marker must lead the utterance or explicitly mention the
        # saved name/value.
        has_correction = normalized.startswith(
            (
                "not ",
                "that's not ",
                "that is not ",
                "that's wrong",
                "that is wrong",
                "incorrect",
                "wrong name",
            )
        ) or any(
            marker in normalized
            for marker in (
                "my name is wrong",
                "the name is wrong",
                "wrong name",
                "my name is not ",
                "the name is not ",
            )
        )
        has_replacement = any(
            marker in normalized for marker in ("it's ", "it is ", "my name is ", "i am ", "i'm ")
        )
        return has_correction and has_replacement

    @staticmethod
    def _room_digits(value: str) -> str | None:
        digit_words = {
            "zero": "0",
            "oh": "0",
            "one": "1",
            "two": "2",
            "to": "2",
            "three": "3",
            "four": "4",
            "five": "5",
            "six": "6",
            "seven": "7",
            "eight": "8",
            "nine": "9",
        }
        tokens = re.findall(r"[a-z]+|\d+", value.lower())
        if not tokens or len(tokens) > 5:
            return None
        if not all(token.isdigit() or token in digit_words for token in tokens):
            return None
        digits = "".join(token if token.isdigit() else digit_words[token] for token in tokens)
        return digits if 2 <= len(digits) <= 5 else None

    @staticmethod
    def _asks_for_name(normalized: str) -> bool:
        return any(
            phrase in normalized for phrase in ("what is my name", "what's my name", "who am i")
        )

    @staticmethod
    def _asks_for_room(normalized: str) -> bool:
        return any(
            phrase in normalized
            for phrase in ("what is my room", "what's my room", "what room am i in")
        )

    @staticmethod
    def _looks_like_room_inquiry(normalized: str) -> bool:
        """Recognise an actual reservation inquiry, not every mention of a room."""

        if re.search(
            r"\b(book|booking|reserve|reservation|availability|available|stay|"
            r"check[- ]?in|check[- ]?out|rate|price)\b",
            normalized,
        ):
            return True
        return bool(
            re.search(
                r"\b(?:find|need|want|looking for)\s+(?:a|an|any)?\s*rooms?\b|"
                r"\bdo you have\s+(?:any\s+)?rooms?\b|"
                r"\b(?:check|show|see)\s+(?:the\s+)?(?:rooms?|availability)\b",
                normalized,
            )
        )

    @staticmethod
    def _append_booking_context(previous: str | None, text: str) -> str:
        value = " ".join(text.split())
        if not previous:
            return value
        return f"{previous} {value}"[-2000:]

    @staticmethod
    def _find_fact(normalized: str, facts: dict[str, str]) -> str | None:
        if not HotelConversationService._is_hotel_fact_question(normalized):
            return None
        aliases = {
            "check-in time": ("check in", "check-in", "arrival"),
            "check-out time": ("check out", "check-out", "departure"),
            "pool hours": ("pool", "swimming"),
            "breakfast hours": ("breakfast",),
            "wifi": ("wifi", "wi-fi", "internet"),
        }
        for topic, terms in aliases.items():
            if topic in facts and any(term in normalized for term in terms):
                return facts[topic]
        return None

    @staticmethod
    def _is_hotel_fact_question(normalized: str) -> bool:
        """Avoid treating booking statements as hotel-policy questions."""

        question_starts = (
            "what ",
            "when ",
            "where ",
            "which ",
            "how ",
            "do you ",
            "can you ",
            "is there ",
            "are there ",
            "does ",
            "would ",
        )
        if normalized.endswith("?") or normalized.startswith(question_starts):
            return True
        # Speech recognition often removes question marks. These compact forms
        # are still unmistakable fact questions, unlike "check-in tomorrow".
        return bool(
            re.fullmatch(
                r"(?:check[- ]?in|check[- ]?out|pool|breakfast|wifi|wi[- ]?fi|internet)\s+"
                r"(?:time|hours?|details?)",
                normalized,
            )
        )

    @staticmethod
    def _extract_booking_slot_update(
        text: str,
        *,
        today: date,
        expected_date_slot: str | None = None,
    ) -> BookingSlotUpdate:
        """Extract explicit booking slots before dialogue routing.

        This intentionally covers the reliable, high-impact voice forms.  The
        deterministic booking rules handle the reliable, high-impact voice
        forms, and must not discard a stated date or party size.
        """

        normalized = " ".join(text.casefold().split())

        month_numbers = {
            "january": 1,
            "jan": 1,
            "february": 2,
            "feb": 2,
            "march": 3,
            "mar": 3,
            "april": 4,
            "apr": 4,
            "may": 5,
            "june": 6,
            "jun": 6,
            "july": 7,
            "jul": 7,
            "august": 8,
            "aug": 8,
            "september": 9,
            "sep": 9,
            "sept": 9,
            "october": 10,
            "oct": 10,
            "november": 11,
            "nov": 11,
            "december": 12,
            "dec": 12,
            "जनवरी": 1,
            "फरवरी": 2,
            "मार्च": 3,
            "अप्रैल": 4,
            "मई": 5,
            "जून": 6,
            "जुलाई": 7,
            "अगस्त": 8,
            "सितंबर": 9,
            "सितम्बर": 9,
            "अक्टूबर": 10,
            "नवंबर": 11,
            "नवम्बर": 11,
            "दिसंबर": 12,
            "दिसम्बर": 12,
        }
        day_numbers = {
            "one": 1,
            "first": 1,
            "two": 2,
            "second": 2,
            "three": 3,
            "third": 3,
            "four": 4,
            "fourth": 4,
            "five": 5,
            "fifth": 5,
            "six": 6,
            "sixth": 6,
            "seven": 7,
            "seventh": 7,
            "eight": 8,
            "eighth": 8,
            "nine": 9,
            "ninth": 9,
            "ten": 10,
            "tenth": 10,
            "eleven": 11,
            "twelve": 12,
            "thirteen": 13,
            "fourteen": 14,
            "fifteen": 15,
            "sixteen": 16,
            "seventeen": 17,
            "eighteen": 18,
            "nineteen": 19,
            "twenty": 20,
            "twenty one": 21,
            "twenty two": 22,
            "twenty three": 23,
            "twenty four": 24,
            "twenty five": 25,
            "twenty six": 26,
            "twenty seven": 27,
            "twenty eight": 28,
            "twenty nine": 29,
            "thirty": 30,
            "thirty one": 31,
            "ek": 1,
            "do": 2,
            "teen": 3,
            "tin": 3,
            "char": 4,
            "chaar": 4,
            "paanch": 5,
            "panch": 5,
            "che": 6,
            "chhe": 6,
            "chhah": 6,
            "saat": 7,
            "aath": 8,
            "ath": 8,
            "nau": 9,
            "das": 10,
            "gyarah": 11,
            "barah": 12,
            "terah": 13,
            "chaudah": 14,
            "pandrah": 15,
            "solah": 16,
            "satrah": 17,
            "atharah": 18,
            "unnis": 19,
            "bees": 20,
            "baais": 22,
            "bais": 22,
            "teis": 23,
            "chaubees": 24,
            "pachis": 25,
            "chhabbees": 26,
            "sattais": 27,
            "atthais": 28,
            "untis": 29,
            "tees": 30,
            "ikattis": 31,
            "एक": 1,
            "दो": 2,
            "तीन": 3,
            "चार": 4,
            "पाँच": 5,
            "पांच": 5,
            "छह": 6,
            "छः": 6,
            "सात": 7,
            "आठ": 8,
            "नौ": 9,
            "दस": 10,
            "ग्यारह": 11,
            "बारह": 12,
            "तेरह": 13,
            "चौदह": 14,
            "पंद्रह": 15,
            "पन्द्रह": 15,
            "सोलह": 16,
            "सत्रह": 17,
            "अठारह": 18,
            "उन्नीस": 19,
            "बीस": 20,
            "इक्कीस": 21,
            "बाईस": 22,
            "बाइस": 22,
            "तेईस": 23,
            "तेइस": 23,
            "चौबीस": 24,
            "पच्चीस": 25,
            "छब्बीस": 26,
            "सत्ताईस": 27,
            "सत्ताइस": 27,
            "अट्ठाईस": 28,
            "अट्ठाइस": 28,
            "उनतीस": 29,
            "तीस": 30,
            "इकतीस": 31,
        }

        def token_pattern(values: dict[str, int]) -> str:
            return "|".join(
                sorted((re.escape(value) for value in values), key=len, reverse=True)
            )

        def parse_day(value: str) -> int | None:
            if value.isdigit():
                parsed = int(value)
            else:
                parsed = day_numbers.get(value)
            return parsed if parsed is not None and 1 <= parsed <= 31 else None

        def parse_absolute_date(day: str, month: str, year: str | None) -> date | None:
            day_value = parse_day(day)
            month_value = month_numbers.get(month)
            if day_value is None or month_value is None:
                return None
            try:
                return date(int(year) if year else today.year, month_value, day_value)
            except ValueError:
                return None

        day_pattern = rf"(?:\d{{1,2}}|{token_pattern(day_numbers)})"
        month_pattern = token_pattern(month_numbers)
        date_mentions = list(
            re.finditer(
                rf"(?P<day>{day_pattern})\s*(?:st|nd|rd|th)?\s*"
                rf"(?P<month>{month_pattern})(?:\s+(?P<year>20\d{{2}}))?"
                rf"|(?P<month_first>{month_pattern})\s+(?P<day_first>{day_pattern})"
                rf"(?:\s*,?\s*(?P<year_first>20\d{{2}}))?",
                normalized,
            )
        )
        absolute_dates: list[date] = []
        for match in date_mentions:
            absolute = parse_absolute_date(
                match.group("day") or match.group("day_first"),
                match.group("month") or match.group("month_first"),
                match.group("year") or match.group("year_first"),
            )
            if absolute:
                absolute_dates.append(absolute)

        check_in = absolute_dates[0] if absolute_dates else None
        check_out = absolute_dates[1] if len(absolute_dates) > 1 else None
        if (
            expected_date_slot == "check_out"
            and len(absolute_dates) == 1
            and not re.search(r"\b(?:check[- ]?in|arrival)\b", normalized)
        ):
            check_in = None
            check_out = absolute_dates[0]
        if check_in and not check_out:
            checkout_match = re.search(
                rf"(?:check[- ]?out|checkout|departure)\b.*?"
                rf"(?P<day>{day_pattern})(?!\s*(?:{month_pattern}))",
                normalized,
            )
            if checkout_match:
                checkout_day = parse_day(checkout_match.group("day"))
                if checkout_day:
                    try:
                        check_out = date(today.year, check_in.month, checkout_day)
                    except ValueError:
                        check_out = None

        def relative_date(value: str) -> date | None:
            phrase = value.strip().removeprefix("the ").removeprefix("a ")
            offsets = {"today": 0, "tomorrow": 1, "day after tomorrow": 2}
            offset = offsets.get(phrase)
            return today.fromordinal(today.toordinal() + offset) if offset is not None else None

        date_phrase = r"(?P<value>(?:the |a )?(?:day after tomorrow|tomorrow|today))"

        def labelled_date(label: str) -> date | None:
            match = re.search(
                rf"\b(?:{label})(?:\s+date)?\s*(?:will be|is|for|on|:)?\s*{date_phrase}\b",
                normalized,
            )
            return relative_date(match.group("value")) if match else None

        labelled_check_in = labelled_date(r"check[- ]?in|arrival")
        labelled_check_out = labelled_date(r"check[- ]?out|checkout|departure")
        if labelled_check_in:
            check_in = labelled_check_in
        if labelled_check_out:
            check_out = labelled_check_out
        if not check_in and not check_out:
            # In an active booking, callers naturally say "rooms for
            # tomorrow" rather than the more formal "my check-in is
            # tomorrow." Treat an unlabelled relative date as check-in; the
            # route guard ensures it becomes booking state only in a booking
            # context, never for an unrelated hotel question.
            unlabelled = re.search(
                r"\b(?P<value>(?:day after tomorrow|tomorrow|today))\b",
                normalized,
            )
            if unlabelled:
                check_in = relative_date(unlabelled.group("value"))

        number_words = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
        }
        count_pattern = r"\d+|one|two|three|four|five|six|seven|eight|nine|ten"
        guest_patterns = (
            rf"\b(?:for|party of)\s+(?P<count>{count_pattern})\b",
            rf"\b(?P<count>{count_pattern})\s*(?:guests?|people|persons?)\b",
            rf"\b(?:the\s+)?(?:guests?|people|persons?)(?:\s+staying)?\s*"
            rf"(?:will be|are|is|:)?\s*(?P<count>{count_pattern})\b",
        )
        guest_count = None
        for pattern in guest_patterns:
            guest_match = re.search(pattern, normalized)
            if guest_match:
                raw_count = guest_match.group("count")
                guest_count = int(raw_count) if raw_count.isdigit() else number_words[raw_count]
                break

        nights = None
        night_match = re.search(
            rf"\b(?:only\s+|for\s+)?(?P<nights>{count_pattern})\s+nights?\b",
            normalized,
        )
        if night_match:
            raw_nights = night_match.group("nights")
            nights = int(raw_nights) if raw_nights.isdigit() else number_words[raw_nights]

        return BookingSlotUpdate(
            check_in=check_in,
            check_out=check_out,
            guest_count=guest_count,
            nights=nights,
        )

    @staticmethod
    def _voice_system_prompt(
        hotel_name: str,
        facts: dict[str, str],
        *,
        caller_name: str | None,
        room_number: str | None,
        active_topic: str | None,
        last_intent: str | None,
        pending_category: str | None,
    ) -> str:
        approved = "\n".join(f"- {topic}: {answer}" for topic, answer in facts.items())
        context = f"- Guest name: {caller_name or 'not captured'}\n- Room: {room_number or 'not captured'}"
        dialogue_state = (
            f"- Active topic: {active_topic or 'general'}\n"
            f"- Last intent: {last_intent or 'unknown'}\n"
            f"- Pending action: {pending_category or 'none'}"
        )
        return (
            f"You are the warm, professional voice concierge for {hotel_name}. "
            "Sound like a real human concierge: acknowledge the guest's latest meaning, answer the latest question first, "
            "and use natural contractions and short pauses. Do not begin every turn with 'Thanks'. "
            "Never repeat a sentence or ask the same question twice in one turn. "
            "Use one complete conversational turn, usually 10 to 24 words: a brief acknowledgement plus the most useful next question or answer. "
            "Do not produce a standalone filler sentence such as 'I can certainly help with that'; combine the acknowledgement with the next step. "
            "Never sound like a form, checklist, or call script. Never invent hotel policies, prices, reservation status, or service status. "
            "Use only these approved hotel facts:\n"
            f"{approved or '- No approved facts are available.'}\n"
            "Guest context captured during this call (not independently verified):\n"
            f"{context}\n"
            "Structured dialogue state for this call (use it to maintain continuity):\n"
            f"{dialogue_state}\n"
            "Start every new call with a general offer to help; do not ask for a room number in the greeting. "
            "If the guest asks for a service request, ask for the room number only when that request needs it. "
            "If a room number is already captured, use it and do not ask the guest to repeat it unless they correct it. "
            "If the guest is vague (for example, 'I need somebody'), ask one open clarification question instead of repeating intake. "
            "If the guest asks a question about the call, their identity, or the hotel, answer that question before collecting a missing slot. "
            "For a room-booking request, acknowledge it and ask for the next missing detail (dates, room type, or number of guests) in the same turn. "
            "When the guest answers a previous question, acknowledge the detail they just gave and move to the next slot; do not restart the flow. "
            "If asked about hotel rules, answer the specific approved fact you know and ask which rule they mean instead of listing unrelated facts. "
            "Use punctuation to make speech sound natural: commas for brief pauses, and one question mark for a question. "
            "Do not collect payment card numbers or claim a real booking; demo reservations are simulated."
        )
