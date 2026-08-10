"""Durable, caller-selectable hotel booking offers.

The interpreter may ask PMS tools for facts, but it never owns the lifecycle
of an offer.  This module is the single boundary between a dated quote and a
real hold: it persists what was shown, invalidates stale choices, and checks
inventory again before reserving anything.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.adapters.hotel_pms import HotelPMSAdapter
from hoa_voice_agent.persistence.hotel_tables import (
    HotelBookingOfferRecord,
    HotelBookingToolEventRecord,
    HotelConversationRecord,
)
from hoa_voice_agent.services.hotel_booking_plan import BookingPlan


class HotelBookingOfferService:
    """Present and accept immutable offers for one hotel conversation."""

    OFFER_DURATION = timedelta(minutes=5)

    def __init__(self, session: AsyncSession, hotel_id: UUID, *, telemetry=None) -> None:
        self.session = session
        self.hotel_id = hotel_id
        self.telemetry = telemetry

    async def present_offer(
        self,
        *,
        conversation: HotelConversationRecord,
        plan: BookingPlan,
        guest_count: int,
        check_in: date,
        check_out: date,
    ) -> dict[str, Any]:
        """Persist one exact, non-reserving option after a PMS quote.

        Presenting a new option supersedes older selectable offers from this
        conversation.  It never releases or alters an already-created hold.
        """

        started = time.perf_counter()
        try:
            quote = await HotelPMSAdapter(self.session, self.hotel_id).search_booking_plan(
                plan,
                guest_count,
                check_in=check_in,
                check_out=check_out,
            )
        finally:
            self._record_validation_time(started)
        if quote["status"] != "available":
            return quote

        await self.invalidate_presented_offers(
            conversation,
            reason="A newer booking option was presented.",
            commit=False,
        )
        checked_at = datetime.now(UTC)
        expires_at = checked_at + self.OFFER_DURATION
        offer = HotelBookingOfferRecord(
            hotel_id=self.hotel_id,
            conversation_id=conversation.id,
            status="presented",
            plan_version=plan.version,
            plan_json=plan.to_json(),
            check_in=check_in,
            check_out=check_out,
            guest_count=guest_count,
            room_lines_json=json.dumps(quote["rooms"], sort_keys=True),
            nightly_total_cents=quote["nightly_total_cents"],
            inventory_checked_at=checked_at,
            expires_at=expires_at,
        )
        self.session.add(offer)
        await self.session.flush()
        conversation.booking_offer_id = offer.id
        await self.session.commit()
        return self._record_as_dict(offer)

    async def accept_offer(
        self,
        *,
        conversation: HotelConversationRecord,
        offer_id: str | UUID,
    ) -> dict[str, Any]:
        """Turn one current offer into a hold after a fresh PMS recheck.

        The offer is deliberately compared by room type, guest allocation and
        price.  A different physical room is acceptable because room numbers
        were not shown to the caller; a material change is not.
        """

        offer = await self._owned_current_offer(conversation, offer_id)
        now = datetime.now(UTC)
        if self._as_utc(offer.expires_at) <= now:
            offer.status = "expired"
            offer.invalidated_reason = "The offer expired before acceptance."
            if conversation.booking_offer_id == offer.id:
                conversation.booking_offer_id = None
            await self.session.commit()
            raise ValueError("That booking option is no longer current. I can check again.")

        plan = BookingPlan.from_json(offer.plan_json)
        started = time.perf_counter()
        try:
            held = await HotelPMSAdapter(self.session, self.hotel_id).create_booking_hold(
                plan,
                offer.guest_count,
                check_in=offer.check_in,
                check_out=offer.check_out,
                existing_hold_id=conversation.booking_hold_reservation_id,
            )
        except ValueError as exc:
            self._record_validation_time(started)
            offer.status = "expired"
            offer.invalidated_reason = "Inventory changed before the offer could be held."
            if conversation.booking_offer_id == offer.id:
                conversation.booking_offer_id = None
            await self.session.commit()
            raise ValueError(
                "That exact booking option is no longer available. I can check current options."
            ) from exc
        else:
            self._record_validation_time(started)
        if self._material_layout(held["rooms"]) != self._material_layout(
            self._room_lines(offer)
        ) or held["nightly_total_cents"] != offer.nightly_total_cents:
            await HotelPMSAdapter(self.session, self.hotel_id).cancel_booking_hold(
                UUID(held["hold_reservation_id"])
            )
            offer.status = "expired"
            offer.invalidated_reason = "Inventory changed before the offer could be held."
            if conversation.booking_offer_id == offer.id:
                conversation.booking_offer_id = None
            await self.session.commit()
            raise ValueError(
                "That exact booking option is no longer available. I can check current options."
            )

        offer.status = "held"
        offer.accepted_at = now
        offer.hold_reservation_id = UUID(held["hold_reservation_id"])
        conversation.booking_hold_reservation_id = offer.hold_reservation_id
        conversation.booking_last_search_result = json.dumps(held, sort_keys=True)
        await self.session.commit()
        return {**self._record_as_dict(offer), "rooms": held["rooms"]}

    async def invalidate_presented_offers(
        self,
        conversation: HotelConversationRecord,
        *,
        reason: str,
        commit: bool = True,
    ) -> None:
        """Make unaccepted options unselectable after a material draft change."""

        rows = list(
            await self.session.scalars(
                select(HotelBookingOfferRecord).where(
                    HotelBookingOfferRecord.hotel_id == self.hotel_id,
                    HotelBookingOfferRecord.conversation_id == conversation.id,
                    HotelBookingOfferRecord.status == "presented",
                )
            )
        )
        for offer in rows:
            offer.status = "superseded"
            offer.invalidated_reason = reason
        if conversation.booking_offer_id in {offer.id for offer in rows}:
            conversation.booking_offer_id = None
        if commit:
            await self.session.commit()

    async def abandon_current_offer(
        self,
        conversation: HotelConversationRecord,
        *,
        reason: str,
        commit: bool = True,
    ) -> None:
        """Remove the caller's selectable option or released hold from use."""

        if not conversation.booking_offer_id:
            return
        offer = await self.session.get(HotelBookingOfferRecord, conversation.booking_offer_id)
        if (
            offer
            and offer.hotel_id == self.hotel_id
            and offer.conversation_id == conversation.id
            and offer.status in {"presented", "held"}
        ):
            offer.status = "cancelled"
            offer.invalidated_reason = reason
        conversation.booking_offer_id = None
        if commit:
            await self.session.commit()

    async def mark_held_offer_confirmed(
        self,
        conversation: HotelConversationRecord,
        *,
        commit: bool = True,
    ) -> None:
        """Close the offer lifecycle once its hold became a reservation."""

        if conversation.booking_offer_id:
            offer = await self.session.get(HotelBookingOfferRecord, conversation.booking_offer_id)
            if (
                offer
                and offer.hotel_id == self.hotel_id
                and offer.conversation_id == conversation.id
                and offer.status == "held"
            ):
                offer.status = "confirmed"
        conversation.booking_offer_id = None
        if commit:
            await self.session.commit()

    async def record_tool_event(
        self,
        *,
        conversation_id: UUID,
        tool_name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Retain the exact PMS evidence used in a booking conversation."""

        self.session.add(
            HotelBookingToolEventRecord(
                hotel_id=self.hotel_id,
                conversation_id=conversation_id,
                tool_name=tool_name,
                arguments_json=json.dumps(arguments, sort_keys=True),
                result_json=json.dumps(result, sort_keys=True),
            )
        )
        await self.session.flush()

    async def get_offer(self, offer_id: str | UUID) -> dict[str, Any] | None:
        """Return a caller-safe snapshot for the persisted offer identifier."""

        try:
            identifier = UUID(str(offer_id))
        except ValueError:
            return None
        record = await self.session.get(HotelBookingOfferRecord, identifier)
        if not record or record.hotel_id != self.hotel_id:
            return None
        return self._record_as_dict(record)

    async def tool_events(self, conversation_id: UUID) -> list[dict[str, Any]]:
        """Return ordered PMS evidence for one booking conversation."""

        rows = list(
            await self.session.scalars(
                select(HotelBookingToolEventRecord)
                .where(
                    HotelBookingToolEventRecord.hotel_id == self.hotel_id,
                    HotelBookingToolEventRecord.conversation_id == conversation_id,
                )
                .order_by(HotelBookingToolEventRecord.created_at, HotelBookingToolEventRecord.id)
            )
        )
        return [
            {
                "tool_name": row.tool_name,
                "arguments": json.loads(row.arguments_json),
                "result": json.loads(row.result_json),
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]

    async def _owned_current_offer(
        self,
        conversation: HotelConversationRecord,
        offer_id: str | UUID,
    ) -> HotelBookingOfferRecord:
        try:
            identifier = UUID(str(offer_id))
        except ValueError as exc:
            raise ValueError(
                "That booking option is no longer current. I can check again."
            ) from exc
        offer = await self.session.get(HotelBookingOfferRecord, identifier)
        if (
            not offer
            or offer.hotel_id != self.hotel_id
            or offer.conversation_id != conversation.id
            or offer.status != "presented"
            or conversation.booking_offer_id != offer.id
        ):
            raise ValueError("That booking option is no longer current. I can check again.")
        return offer

    @staticmethod
    def _room_lines(record: HotelBookingOfferRecord) -> list[dict[str, Any]]:
        try:
            value = json.loads(record.room_lines_json)
        except json.JSONDecodeError:
            return []
        return value if isinstance(value, list) else []

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """Normalize SQLite's naive timestamps before comparing expiry."""

        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @staticmethod
    def _material_layout(lines: list[dict[str, Any]]) -> list[tuple[str, int, int]]:
        return sorted(
            (
                str(line["room_type"]).casefold(),
                int(line["guests"]),
                int(line["nightly_rate_cents"]),
            )
            for line in lines
        )

    def _record_as_dict(self, record: HotelBookingOfferRecord) -> dict[str, Any]:
        return {
            "offer_id": str(record.id),
            "status": record.status,
            "plan_version": record.plan_version,
            "check_in": record.check_in.isoformat(),
            "check_out": record.check_out.isoformat(),
            "guest_count": record.guest_count,
            "rooms": self._room_lines(record),
            "nightly_total_cents": record.nightly_total_cents,
            "inventory_checked_at": record.inventory_checked_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
            "hold_reservation_id": (
                str(record.hold_reservation_id) if record.hold_reservation_id else None
            ),
        }

    def _record_validation_time(self, started: float) -> None:
        if self.telemetry:
            self.telemetry.record(
                "pms_validation_ms",
                (time.perf_counter() - started) * 1000,
            )
