"""Booking-specific orchestration behind a small conversation seam.

The coordinator owns the booking workflow's language and lifecycle execution.
It uses the PMS and offer services for side effects, but the general hotel
conversation service no longer needs to know how an offer becomes a hold or
how a hold becomes a confirmed reservation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.adapters.hotel_pms import HotelPMSAdapter
from hoa_voice_agent.persistence.hotel_repository import HotelStore
from hoa_voice_agent.services.hotel_booking_flow import HotelBookingFlow
from hoa_voice_agent.services.hotel_booking_offers import HotelBookingOfferService
from hoa_voice_agent.services.hotel_booking_plan import (
    BookingPlan,
    PartyGroup,
    RoomTypeRequest,
    room_split_prompt,
    room_type_prompt,
)
from hoa_voice_agent.services.hotel_catalog import CatalogSnapshotService
from hoa_voice_agent.voice.language import (
    LanguageAdapter,
)
from hoa_voice_agent.voice.language import (
    language_adapter as build_language_adapter,
)

ConfirmationNotifier = Callable[..., Awaitable[bool]]


@dataclass(frozen=True)
class BookingCoordinatorResponse:
    intent: str
    say: str
    request_id: UUID | None = None


class HotelBookingCoordinator:
    """Deep booking module with a small caller-facing interface."""

    def __init__(
        self,
        session: AsyncSession,
        hotel_id: UUID,
        *,
        booking_flow: HotelBookingFlow | None = None,
        confirmation_notifier: ConfirmationNotifier | None = None,
        language_adapter: LanguageAdapter | None = None,
        telemetry: Any | None = None,
    ) -> None:
        self.session = session
        self.hotel_id = hotel_id
        self.store = HotelStore(session)
        self.booking_flow = booking_flow or HotelBookingFlow()
        self._confirmation_notifier = confirmation_notifier
        self.telemetry = telemetry
        self.language_adapter = language_adapter or build_language_adapter(
            "en",
            english_voice="",
            hindi_voice="",
        )

    def _text(self, key: str, **values: object) -> str:
        return self.language_adapter.text(key, **values)

    async def present_current_offer(self, conversation) -> str | None:
        """Create a non-reserving offer for the current exact caller layout."""

        if not (
            conversation.booking_check_in
            and conversation.booking_check_out
            and conversation.booking_guest_count
        ):
            return None
        plan = BookingPlan.from_json(conversation.booking_plan_json)
        if not plan.has_exact_allocation:
            return None
        offer = await HotelBookingOfferService(
            self.session, self.hotel_id, telemetry=self.telemetry
        ).present_offer(
            conversation=conversation,
            plan=plan,
            guest_count=conversation.booking_guest_count,
            check_in=conversation.booking_check_in,
            check_out=conversation.booking_check_out,
        )
        if offer["status"] != "presented":
            conversation.state = self.booking_flow.offer_state(presented=False)
            if offer.get("failure_code") == "party_split_required" and plan.party_groups:
                return room_split_prompt(plan)
            if offer.get("failure_code") == "insufficient_capacity":
                plan_lines = [
                    {"room_type": request.room_type}
                    for request in plan.allocation
                    for _ in range(request.quantity)
                ]
                layout = self._spoken_room_summary(plan_lines)
                capacity = offer.get("requested_total_capacity")
                guests = offer.get("guest_count")
                recommendations = offer.get("alternative_recommendations")
                if (
                    isinstance(capacity, int)
                    and isinstance(guests, int)
                    and isinstance(recommendations, list)
                    and recommendations
                    and isinstance(recommendations[0], dict)
                ):
                    recommendation_response = await self.present_recommended_offer(
                        conversation,
                        recommendation=recommendations[0],
                        available_room_types=offer.get("available_room_types"),
                        opening=(
                            self._text(
                                "offer_party_split",
                                layout=layout.capitalize(),
                                capacity=capacity,
                                guests=guests,
                            )
                        ),
                    )
                    if recommendation_response:
                        return recommendation_response
            return self._text(
                "offer_exact_failure",
                reason=offer["reason"],
            )
        lines = offer["rooms"]
        room_description = " plus ".join(f"1 {line['room_type']} room" for line in lines)
        allocation_description = ", ".join(
            f"{line['guests']} guest{'s' if line['guests'] != 1 else ''} in {line['room_type']} "
            f"at ${line['nightly_rate_cents'] / 100:.2f} per night"
            for line in lines
            if line["guests"] is not None
        )
        nightly_total = offer["nightly_total_cents"] / 100
        conversation.booking_last_search_result = json.dumps(offer, sort_keys=True)
        conversation.state = self.booking_flow.offer_state(presented=True)
        return self._text(
            "offer_exact",
            room_description=room_description,
            allocation_description=allocation_description,
            nightly_total=nightly_total,
        )

    async def present_recommended_offer(
        self,
        conversation,
        *,
        recommendation: dict[str, Any],
        available_room_types: object,
        opening: str,
        include_available_counts: bool = True,
    ) -> str | None:
        """Persist one PMS-suggested layout without changing caller-owned plan."""

        allocation = recommendation.get("allocation")
        room_lines = recommendation.get("room_lines")
        if not isinstance(allocation, list) or not isinstance(room_lines, list):
            return None
        room_requests: list[RoomTypeRequest] = []
        for item in allocation:
            if not isinstance(item, dict):
                return None
            room_type = item.get("room_type")
            quantity = item.get("quantity")
            if not isinstance(room_type, str) or not isinstance(quantity, int) or quantity < 1:
                return None
            room_requests.append(RoomTypeRequest(room_type=room_type, quantity=quantity))
        party_groups: list[PartyGroup] = []
        for index, line in enumerate(room_lines, start=1):
            if not isinstance(line, dict):
                return None
            guests = line.get("guests")
            if not isinstance(guests, int) or guests < 1:
                return None
            party_groups.append(
                PartyGroup(label=f"recommended room {index}", people_per_group=guests)
            )
        if not room_requests or not party_groups:
            return None
        if not (
            conversation.booking_check_in
            and conversation.booking_check_out
            and conversation.booking_guest_count
        ):
            return None

        plan = BookingPlan(
            version=conversation.booking_plan_version,
            party_groups=party_groups,
            allocation=room_requests,
            allow_room_sharing=False,
        )
        offer = await HotelBookingOfferService(
            self.session, self.hotel_id, telemetry=self.telemetry
        ).present_offer(
            conversation=conversation,
            plan=plan,
            guest_count=conversation.booking_guest_count,
            check_in=conversation.booking_check_in,
            check_out=conversation.booking_check_out,
        )
        if offer.get("status") != "presented":
            return None

        conversation.booking_last_search_result = json.dumps(offer, sort_keys=True)
        conversation.state = self.booking_flow.offer_state(presented=True)
        available_summary = (
            self._spoken_room_type_summary(available_room_types)
            if include_available_counts
            else self._spoken_room_type_names(available_room_types)
        )
        layout_summary = self._spoken_room_summary(offer.get("rooms"))
        split_summary = self._guest_split_summary(offer.get("rooms"))
        nightly_total = offer["nightly_total_cents"] / 100
        availability_sentence = (
            f" I have {available_summary} available." if available_summary else ""
        )
        split_sentence = f" The split is {split_summary}." if split_summary else ""
        return self._text(
            "offer_recommended",
            opening=opening,
            availability_sentence=availability_sentence,
            layout_summary=layout_summary,
            nightly_total=nightly_total,
            split_sentence=split_sentence,
        )

    async def recommended_offer_response(self, conversation) -> str | None:
        """Turn current multi-room PMS evidence into a caller-selectable offer."""

        try:
            stored = json.loads(conversation.booking_last_search_result or "{}")
        except json.JSONDecodeError:
            return None
        if stored.get("tool") != "search_rooms" or not isinstance(stored.get("result"), dict):
            return None
        result = stored["result"]
        recommendations = result.get("recommendations")
        if isinstance(recommendations, list) and recommendations:
            recommendation = recommendations[0]
            if isinstance(recommendation, dict):
                reason = recommendation.get("explanation")
                if not isinstance(reason, str) or not reason:
                    reason = "it matches the requirements you shared"
                return await self.present_recommended_offer(
                    conversation,
                    recommendation=recommendation,
                    available_room_types=result.get("available_room_types"),
                    opening=self._text("recommendation_opening", reason=reason),
                    include_available_counts=False,
                )
        if result.get("rooms"):
            return None
        recommendations = result.get("multi_room_recommendations")
        if not isinstance(recommendations, list) or not recommendations:
            return None
        recommendation = recommendations[0]
        if not isinstance(recommendation, dict):
            return None
        date_range = self._booking_date_range(
            conversation.booking_check_in,
            conversation.booking_check_out,
        )
        return await self.present_recommended_offer(
            conversation,
            recommendation=recommendation,
            available_room_types=result.get("available_room_types"),
            opening=self._text(
                "offer_no_single_room",
                guest_count=conversation.booking_guest_count,
                date_range=date_range,
            ),
            include_available_counts=False,
        )

    def verified_room_search_response(
        self,
        conversation,
        *,
        comparison: bool = False,
    ) -> str | None:
        """Render current dated inventory evidence without inventing facts."""

        try:
            stored = json.loads(conversation.booking_last_search_result or "{}")
        except json.JSONDecodeError:
            return None
        if stored.get("tool") != "search_rooms" or not isinstance(stored.get("result"), dict):
            return None

        result = stored["result"]
        if isinstance(result.get("criteria_failure"), str):
            return self._text(
                "search_criteria_none",
                reason=result["criteria_failure"],
            )
        requested_type = result.get("requested_room_type")
        status = result.get("requested_room_type_status")
        check_in = self._friendly_booking_date(result.get("check_in"))
        check_out = self._friendly_booking_date(result.get("check_out"))
        date_range = (
            f"{check_in} through {check_out}" if check_in and check_out else "those dates"
        )
        guest_count = result.get("guest_count")
        alternatives = result.get("alternative_room_types")
        alternative_summary = self._room_type_summary(alternatives)

        if isinstance(requested_type, str) and requested_type:
            room_label = f"{requested_type.title()} rooms"
            if status == "sold_out":
                if alternative_summary:
                    singular = self._singular_room_type(alternatives)
                    if comparison:
                        return self._text(
                            "search_sold_out_compare",
                            date_range=date_range,
                            room_label=room_label.lower(),
                            alternative_summary=alternative_summary,
                        )
                    return self._text(
                        "search_sold_out",
                        room_label=room_label,
                        date_range=date_range,
                        alternative_summary=alternative_summary,
                        singular=singular,
                    )
                return self._text(
                    "search_sold_out_none",
                    room_label=room_label,
                    date_range=date_range,
                )
            if status == "does_not_fit_party":
                guest_label = (
                    f"{guest_count} guests" if isinstance(guest_count, int) else "your party"
                )
                if alternative_summary:
                    singular = self._singular_room_type(alternatives)
                    if comparison:
                        return self._text(
                            "search_capacity_compare",
                            date_range=date_range,
                            room_label=room_label.lower(),
                            guest_label=guest_label,
                            alternative_summary=alternative_summary,
                        )
                    return self._text(
                        "search_capacity",
                        room_label=room_label,
                        guest_label=guest_label,
                        date_range=date_range,
                        alternative_summary=alternative_summary,
                        singular=singular,
                    )
                return self._text(
                    "search_capacity_none",
                    room_label=room_label,
                    guest_label=guest_label,
                    date_range=date_range,
                )
            if status == "available":
                count = len(result.get("rooms") or [])
                if count:
                    return self._text(
                        "search_available",
                        room_type_label=self._room_type_label(requested_type),
                        date_range=date_range,
                    )
            if status == "unknown_room_type":
                return self._text(
                    "search_unknown_type",
                    requested_type=requested_type,
                )

        room_summary = self._room_type_summary_from_rooms(result.get("rooms"))
        if room_summary:
            if comparison:
                return self._text(
                    "search_compare",
                    date_range=date_range,
                    room_summary=room_summary,
                )
            return self._text(
                "search_options",
                date_range=date_range,
                room_summary=room_summary,
            )
        return self._text(
            "search_none",
            date_range=date_range,
        )

    def verified_room_catalog_response(self, conversation) -> str | None:
        """Render catalogue/detail tool evidence without model narration."""

        try:
            stored = json.loads(conversation.booking_last_search_result or "{}")
        except json.JSONDecodeError:
            return None
        tool = stored.get("tool")
        result = stored.get("result")
        if not isinstance(result, dict):
            return None
        if tool == "list_room_types":
            room_types = result.get("room_types")
            if not isinstance(room_types, list):
                return None
            names = sorted(
                {
                    str(room_type).title()
                    for room_type in room_types
                    if isinstance(room_type, str) and room_type.strip()
                }
            )
            if not names:
                return self._text("room_details_unavailable")
            return self._text("booking_options", room_types=", ".join(names))
        if tool == "get_room_details":
            rooms = result.get("rooms")
            if not isinstance(rooms, list) or not rooms:
                return self._text("room_details_unavailable")
            rendered: list[str] = []
            seen: set[tuple[str, int, int]] = set()
            scope = stored.get("response_scope") or "room_details"
            for room in rooms:
                if not isinstance(room, dict):
                    continue
                room_type = room.get("room_type")
                capacity = room.get("max_occupancy")
                nightly_rate = room.get("nightly_rate_cents")
                if not isinstance(room_type, str):
                    continue
                if not isinstance(capacity, int) or not isinstance(nightly_rate, int):
                    continue
                signature = (room_type, capacity, nightly_rate)
                if signature in seen:
                    continue
                seen.add(signature)
                if scope == "feature_match":
                    amenities = [
                        item.get("name")
                        for item in room.get("amenities", [])
                        if isinstance(item, dict) and isinstance(item.get("name"), str)
                    ]
                    if amenities:
                        rendered.append(
                            self._text(
                                "room_details_features",
                                room_type=room_type,
                                amenities=self._spoken_list(amenities),
                            )
                        )
                    else:
                        rendered.append(self._text("room_details_unavailable"))
                elif scope == "price":
                    rendered.append(
                        self._text(
                            "room_details_price",
                            room_type=room_type,
                            price=nightly_rate / 100,
                        )
                    )
                elif scope == "capacity":
                    rendered.append(
                        self._text(
                            "room_details_capacity",
                            room_type=room_type,
                            capacity=capacity,
                            bed_type=room.get("bed_type") or "the configured",
                        )
                    )
                else:
                    rendered.append(
                        self._text(
                            "room_details",
                            room_type=room_type,
                            capacity=capacity,
                            price=nightly_rate / 100,
                        )
                    )
            return " ".join(rendered) if rendered else self._text("room_details_unavailable")
        return None

    async def accept_current_offer(self, conversation) -> BookingCoordinatorResponse:
        """Accept the current option through the durable offer service."""

        if not conversation.booking_offer_id:
            if not conversation.booking_hold_reservation_id:
                conversation.state = "collecting_booking_allocation"
                say = self._text("offer_stale")
            else:
                conversation.state = "awaiting_booking_details"
                say = self._text("offer_proceed")
        else:
            try:
                await HotelBookingOfferService(
                    self.session, self.hotel_id, telemetry=self.telemetry
                ).accept_offer(
                    conversation=conversation,
                    offer_id=conversation.booking_offer_id,
                )
            except ValueError as exc:
                conversation.booking_hold_reservation_id = None
                conversation.booking_last_search_result = None
                conversation.state = "collecting_booking_allocation"
                say = self._text("hold_error", error=str(exc))
            else:
                conversation.state = "awaiting_booking_details"
                say = self._text("offer_held")
        await self.store.add_turn(conversation.id, "assistant", say)
        await self.session.commit()
        return BookingCoordinatorResponse("booking_details", say)

    async def confirm_current_hold(self, conversation) -> BookingCoordinatorResponse:
        """Confirm the active hold and finish the caller-facing booking flow."""

        if not conversation.booking_hold_reservation_id:
            conversation.state = "collecting_booking_allocation"
            say = self._text("hold_stale")
        else:
            try:
                confirmed = await HotelPMSAdapter(
                    self.session, self.hotel_id
                ).confirm_booking_hold(
                    conversation.booking_hold_reservation_id,
                    guest_name=conversation.caller_name,
                    phone=conversation.caller_phone,
                )
            except ValueError as exc:
                conversation.booking_hold_reservation_id = None
                conversation.booking_last_search_result = None
                conversation.state = "collecting_booking_allocation"
                say = self._text("hold_error", error=str(exc))
            else:
                conversation.reservation_id = UUID(confirmed["reservation_id"])
                conversation.booking_hold_reservation_id = None
                await HotelBookingOfferService(
                    self.session, self.hotel_id, telemetry=self.telemetry
                ).mark_held_offer_confirmed(conversation, commit=False)
                conversation.pending_category = None
                conversation.pending_description = None
                room_count = len(confirmed["rooms"])
                room_summary = self._booking_room_summary(confirmed["rooms"])
                whatsapp_delivered = False
                if self._confirmation_notifier:
                    whatsapp_delivered = await self._confirmation_notifier(
                        conversation=conversation,
                        confirmed=confirmed,
                        room_summary=room_summary,
                    )
                conversation.state = "awaiting_post_action_followup"
                date_range = (
                    f"{self._format_booking_date(conversation.booking_check_in)} through "
                    f"{self._format_booking_date(conversation.booking_check_out)}"
                )
                booking_verb = "is" if room_count == 1 else "are"
                delivery_note = (
                    "The details will be delivered to you shortly through WhatsApp."
                    if whatsapp_delivered
                    else (
                        "Your booking is confirmed, but I could not send the WhatsApp details "
                        "right now."
                    )
                )
                say = self._text(
                    "booking_confirmed",
                    caller_name=conversation.caller_name or "guest",
                    room_summary=room_summary,
                    booking_verb=booking_verb,
                    date_range=date_range,
                    confirmation_code=confirmed["confirmation_code"],
                    delivery_note=delivery_note,
                )
        await self.store.add_turn(conversation.id, "assistant", say)
        await self.session.commit()
        return BookingCoordinatorResponse("booking_confirmed", say)

    async def cancel_unfinished_booking(
        self,
        conversation,
    ) -> BookingCoordinatorResponse:
        """Release an offer/hold without touching a confirmed reservation."""

        had_hold = bool(conversation.booking_hold_reservation_id)
        if conversation.booking_hold_reservation_id:
            await HotelPMSAdapter(self.session, self.hotel_id).cancel_booking_hold(
                conversation.booking_hold_reservation_id
            )
            conversation.booking_hold_reservation_id = None
        await HotelBookingOfferService(
            self.session, self.hotel_id, telemetry=self.telemetry
        ).abandon_current_offer(
            conversation,
            reason="The caller cancelled the booking.",
            commit=False,
        )
        conversation.booking_last_search_result = None
        conversation.booking_catalog_snapshot_version = None
        conversation.booking_catalog_relevant_json = None
        conversation.booking_criteria_json = None
        conversation.booking_criteria_revision = 0
        conversation.booking_response_scope = None
        conversation.pending_category = None
        conversation.pending_description = None
        conversation.state = "ready"
        say = self._text("cancelled_hold" if had_hold else "cancelled_offer")
        await self.store.add_turn(conversation.id, "assistant", say)
        await self.session.commit()
        return BookingCoordinatorResponse("booking_cancelled", say)

    async def fallback_response(self, conversation, text: str) -> str:
        """Advance a legacy/non-LLM booking plan to its next safe step."""

        context = f"{conversation.pending_description or ''} {text}".casefold()
        guest_match = re.search(r"\b(\d+)\s*(?:guest|guests|people|persons)\b", context)
        guest_count = conversation.booking_guest_count or (
            int(guest_match.group(1)) if guest_match else None
        )
        pms = HotelPMSAdapter(self.session, self.hotel_id)
        plan = BookingPlan.from_json(conversation.booking_plan_json)
        plan_changed = plan.apply_caller_turn(text, await pms.room_types())
        current_text = text.casefold()
        no_couples_answer = bool(
            re.search(r"\bno\s+couples?\b", current_text)
            or re.search(r"\b(?:none|nah|nahi|nahin)\b", current_text)
        )
        if (
            guest_count
            and guest_count > 1
            and not plan.party_composition_known
            and not plan.party_groups
            and no_couples_answer
        ):
            plan.party_composition_known = True
            plan.version += 1
            plan_changed = True
        if plan_changed:
            if conversation.booking_hold_reservation_id:
                await pms.cancel_booking_hold(conversation.booking_hold_reservation_id)
                conversation.booking_hold_reservation_id = None
            await HotelBookingOfferService(
                self.session, self.hotel_id, telemetry=self.telemetry
            ).invalidate_presented_offers(
                conversation,
                reason="The caller changed the requested room layout.",
                commit=False,
            )
            conversation.booking_plan_json = plan.to_json()
            conversation.booking_plan_version = plan.version
            conversation.booking_last_search_result = None
            conversation.state = "collecting_booking_allocation"
        if (
            guest_count
            and guest_count > 1
            and not plan.party_composition_known
            and not plan.has_exact_allocation
        ):
            return self._text("booking_party_composition", guest_count=guest_count)
        if not conversation.booking_check_in and not conversation.booking_check_out:
            return self._text("booking_dates")
        if not conversation.booking_check_in:
            return self._text("booking_check_in")
        if not conversation.booking_check_out:
            return self._text("booking_check_out")
        if guest_count is None:
            return self._text("booking_guest_count")

        if plan.party_size is not None and plan.party_size != guest_count:
            return self._text(
                "booking_party_mismatch",
                party_size=plan.party_size,
                guest_count=guest_count,
            )
        if (
            plan.party_composition_known
            and plan.party_groups
            and not plan.has_exact_allocation
        ):
            result = await pms.search_booking_availability(
                check_in=conversation.booking_check_in,
                check_out=conversation.booking_check_out,
                guest_count=guest_count,
                booking_plan=plan,
            )
            conversation.booking_last_search_result = json.dumps(
                {
                    "tool": "search_rooms",
                    "arguments": {
                        "check_in": conversation.booking_check_in.isoformat(),
                        "check_out": conversation.booking_check_out.isoformat(),
                        "guest_count": guest_count,
                    },
                    "result": result,
                },
                sort_keys=True,
            )
            recommended = await self.recommended_offer_response(conversation)
            if recommended:
                return recommended
        if guest_count > 1 and not plan.party_composition_known and not plan.has_exact_allocation:
            return self._text("booking_party_composition", guest_count=guest_count)
        if plan.party_size is not None and plan.allow_room_sharing is None:
            return room_split_prompt(plan)
        if plan.party_size is not None and plan.room_count is None:
            return self._text("booking_room_count")
        if plan.requested_room_count is not None and not plan.has_exact_allocation:
            if plan.allow_mixed_types:
                return self._text("booking_mixed_types")
            return room_type_prompt(plan)
        if not plan.has_exact_allocation:
            return self._text("booking_room_type")
        offer_response = await self.present_current_offer(conversation)
        assert offer_response is not None
        return offer_response

    def progressive_booking_prompt(self, conversation) -> str | None:
        """Return only the next missing booking question from current state."""

        if not conversation.booking_check_in and not conversation.booking_check_out:
            return self._text("booking_dates_only")
        if not conversation.booking_check_in:
            return self._text("booking_check_in")
        if not conversation.booking_check_out:
            return self._text("booking_check_out")
        if not conversation.booking_guest_count:
            return self._text("booking_guest_count")

        plan = BookingPlan.from_json(conversation.booking_plan_json)
        if (
            conversation.booking_guest_count > 1
            and not plan.party_composition_known
            and not plan.has_exact_allocation
        ):
            return self._text(
                "booking_party_composition",
                guest_count=conversation.booking_guest_count,
            )
        if plan.party_size is not None and plan.party_size != conversation.booking_guest_count:
            return self._text(
                "booking_party_mismatch",
                party_size=plan.party_size,
                guest_count=conversation.booking_guest_count,
            )
        if plan.party_size is not None and plan.allow_room_sharing is None:
            return room_split_prompt(plan)
        if plan.party_size is not None and plan.requested_room_count is None:
            return self._text("booking_room_count")
        if plan.requested_room_count is not None and not plan.has_exact_allocation:
            if plan.allow_mixed_types:
                return self._text("booking_mixed_types")
            return room_type_prompt(plan)
        if not plan.has_exact_allocation:
            return self._text("booking_room_type")
        return None

    async def options_response(self, conversation, text: str) -> str:
        """Answer an alternatives question without changing the selected plan."""

        context = f"{conversation.pending_description or ''} {text}".casefold()
        match = re.search(r"\b(\d+)\s*(?:guest|guests|people|persons)\b", context)
        guest_count = conversation.booking_guest_count or (
            int(match.group(1)) if match else None
        )
        if guest_count is None:
            return self._text("booking_compare_guest_count")
        plan = BookingPlan.from_json(conversation.booking_plan_json)
        if guest_count > 1 and not plan.party_composition_known:
            return self._text("booking_party_composition", guest_count=guest_count)
        availability = await HotelPMSAdapter(self.session, self.hotel_id).search_availability()
        fitting = [room for room in availability if int(room["max_occupancy"]) >= guest_count]
        if not fitting:
            return self._text("booking_no_single_room")
        types = sorted({room["room_type"] for room in fitting})
        if len(types) == 1:
            return self._text(
                "booking_one_type",
                guest_count=guest_count,
                room_type=types[0],
            )
        return self._text("booking_options", room_types=", ".join(types))

    async def information_response(
        self,
        conversation,
        text: str,
        normalized: str,
    ) -> tuple[str, str] | None:
        """Answer booking side questions without discarding booking state."""

        room_detail = await self.room_detail_response(text)
        if room_detail:
            return "booking_detail", room_detail
        facts = await self.store.active_facts(self.hotel_id)
        fact_answer = self._find_fact(normalized, facts)
        if fact_answer:
            return "faq", fact_answer
        return None

    async def room_detail_response(self, text: str) -> str | None:
        normalized = text.casefold()
        if not re.search(
            r"\b(include|included|amenit(?:y|ies)|features?|details?|describe|"
            r"tell me about|how many|capacity|sleep|fit|people|guests?|rate|price)\b",
            normalized,
        ):
            return None
        catalog = await CatalogSnapshotService(self.session, self.hotel_id).get()
        room_types = sorted(catalog.room_type_keys, key=len, reverse=True)
        room_type = next(
            (
                candidate
                for candidate in room_types
                if re.search(rf"\b{re.escape(candidate.casefold())}\b", normalized)
            ),
            None,
        )
        if not room_type:
            named_room = re.search(r"\b(?:the|a|an)\s+([a-z][a-z0-9-]*)\s+room\b", normalized)
            if named_room:
                requested = named_room.group(1)
                return self._text(
                    "room_not_found",
                    requested=requested,
                    room_types=" or ".join(room_types),
                )
            if room_types:
                return self._text(
                    "room_details_prompt",
                    room_types=" or ".join(room_types),
                )
            return self._text("room_details_unavailable")
        entry = catalog.find_room_type(room_type)
        if not entry:
            return self._text("room_details_unavailable")
        primary = entry.primary_rate_plan
        price = primary.nightly_rate_cents / 100 if primary else 0
        # This path is used only when the LLM interpreter is unavailable. The
        # semantic scope normally arrives with the same interpreted tool call.
        scope = "room_details"
        if scope == "feature_match":
            names = [amenity.name for amenity in entry.amenities]
            return (
                self._text(
                    "room_details_features",
                    room_type=entry.display_name,
                    amenities=self._spoken_list(names),
                )
                if names
                else self._text("room_details_unavailable")
            )
        if scope == "price":
            return self._text(
                "room_details_price",
                room_type=entry.display_name,
                price=price,
            )
        if scope == "capacity":
            return self._text(
                "room_details_capacity",
                room_type=entry.display_name,
                capacity=entry.max_occupancy,
                bed_type=entry.bed_type or "the configured",
            )
        return self._text(
            "room_details",
            room_type=entry.display_name,
            capacity=entry.max_occupancy,
            price=price,
        )

    @staticmethod
    def _find_fact(normalized: str, facts: dict[str, str]) -> str | None:
        aliases = {
            "check-in time": ("check in", "check-in", "arrival"),
            "check-out time": ("check out", "check-out", "departure"),
            "pool hours": ("pool", "swimming"),
            "breakfast hours": ("breakfast",),
            "wifi": ("wifi", "wi-fi", "internet"),
        }
        if not (
            normalized.endswith("?")
            or normalized.startswith(
                ("what ", "when ", "where ", "which ", "how ", "do you ", "can you ")
            )
        ):
            return None
        for topic, terms in aliases.items():
            if topic in facts and any(term in normalized for term in terms):
                return facts[topic]
        return None

    @staticmethod
    def _format_booking_date(value: date | None) -> str:
        if not value:
            return "the selected dates"
        return f"{value.strftime('%B')} {value.day}, {value.year}"

    @staticmethod
    def _friendly_booking_date(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
        return f"{parsed.strftime('%B')} {parsed.day}"

    @classmethod
    def _booking_date_range(cls, check_in: date, check_out: date) -> str:
        return (
            f"{cls._friendly_booking_date(check_in.isoformat())} through "
            f"{cls._friendly_booking_date(check_out.isoformat())}"
        )

    @staticmethod
    def _booking_room_summary(rooms: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for room in rooms:
            room_type = str(room.get("room_type") or "room")
            counts[room_type] = counts.get(room_type, 0) + 1
        return " and ".join(
            f"{count} {room_type.title()} room{'s' if count != 1 else ''}"
            for room_type, count in counts.items()
        ) or "the selected rooms"

    @staticmethod
    def _spoken_list(items: list[str]) -> str:
        if not items:
            return ""
        if len(items) == 1:
            return items[0]
        if len(items) == 2:
            return f"{items[0]} and {items[1]}"
        return f"{', '.join(items[:-1])}, and {items[-1]}"

    @classmethod
    def _spoken_room_type_summary(cls, room_types: object) -> str:
        if not isinstance(room_types, list):
            return ""
        return cls._spoken_list(
            [
                cls._counted_room_type(item["available_room_count"], item["room_type"])
                for item in room_types
                if isinstance(item, dict)
                and isinstance(item.get("room_type"), str)
                and isinstance(item.get("available_room_count"), int)
                and item["available_room_count"] > 0
            ]
        )

    @classmethod
    def _spoken_room_type_names(cls, room_types: object) -> str:
        if not isinstance(room_types, list):
            return ""
        names = sorted(
            {
                str(item.get("room_type")).title()
                for item in room_types
                if isinstance(item, dict) and isinstance(item.get("room_type"), str)
            }
        )
        return cls._spoken_list(names)

    @classmethod
    def _spoken_room_summary(cls, rooms: object) -> str:
        if not isinstance(rooms, list):
            return ""
        counts: dict[str, int] = {}
        for room in rooms:
            if isinstance(room, dict) and isinstance(room.get("room_type"), str):
                counts[room["room_type"]] = counts.get(room["room_type"], 0) + 1
        return cls._spoken_list(
            [
                cls._counted_room_type(count, room_type)
                for room_type, count in sorted(counts.items())
            ]
        )

    @classmethod
    def _guest_split_summary(cls, rooms: object) -> str:
        if not isinstance(rooms, list):
            return ""
        return cls._spoken_list(
            [
                f"{room['guests']} guest{'s' if room['guests'] != 1 else ''} in the "
                f"{room['room_type']}"
                for room in rooms
                if isinstance(room, dict)
                and isinstance(room.get("room_type"), str)
                and isinstance(room.get("guests"), int)
            ]
        )

    @staticmethod
    def _counted_room_type(count: int, room_type: str) -> str:
        return f"{count} {room_type}" + (" room" if count == 1 else " rooms")

    @staticmethod
    def _room_type_label(room_type: str) -> str:
        """Name a room type without exposing its internal inventory count."""

        return f"{room_type.title()} rooms"

    @staticmethod
    def _room_type_summary(alternatives: object) -> str:
        if not isinstance(alternatives, list):
            return ""
        descriptions: list[str] = []
        for alternative in alternatives:
            if not isinstance(alternative, dict):
                continue
            room_type = alternative.get("room_type")
            count = alternative.get("available_room_count")
            if isinstance(room_type, str) and isinstance(count, int) and count > 0:
                descriptions.append(HotelBookingCoordinator._counted_room_type(count, room_type))
        return ", ".join(descriptions)

    @staticmethod
    def _singular_room_type(alternatives: object) -> str:
        if isinstance(alternatives, list):
            for alternative in alternatives:
                if isinstance(alternative, dict) and isinstance(
                    alternative.get("room_type"), str
                ):
                    return f"a {alternative['room_type']}"
        return "one of those rooms"

    @staticmethod
    def _room_type_summary_from_rooms(rooms: object) -> str:
        if not isinstance(rooms, list):
            return ""
        room_types: set[str] = set()
        for room in rooms:
            if isinstance(room, dict) and isinstance(room.get("room_type"), str):
                room_types.add(room["room_type"].title())
        return HotelBookingCoordinator._spoken_list(sorted(room_types))


__all__ = ["BookingCoordinatorResponse", "HotelBookingCoordinator"]
