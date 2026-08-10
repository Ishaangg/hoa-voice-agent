"""Small repository layer for the separate hotel domain."""

# ruff: noqa: E501

import json
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hoa_voice_agent.persistence.hotel_tables import (
    HotelConversationRecord,
    HotelConversationTurnRecord,
    HotelDepartmentRecord,
    HotelGuestRecord,
    HotelKnowledgeRecord,
    HotelMenuItemRecord,
    HotelRecord,
    HotelRequestActionRecord,
    HotelRequestRecord,
    HotelReservationRecord,
    HotelRoomRecord,
)
from hoa_voice_agent.persistence.repository import normalize_phone_number


class HotelStore:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def hotel(self, hotel_id: UUID) -> HotelRecord | None:
        return await self.session.get(HotelRecord, hotel_id)

    async def active_facts(self, hotel_id: UUID) -> dict[str, str]:
        rows = await self.session.scalars(
            select(HotelKnowledgeRecord)
            .where(
                HotelKnowledgeRecord.hotel_id == hotel_id,
                HotelKnowledgeRecord.is_active.is_(True),
                HotelKnowledgeRecord.approval_status == "approved",
            )
            .order_by(HotelKnowledgeRecord.category, HotelKnowledgeRecord.topic)
        )
        return {row.topic: row.answer for row in rows}

    async def menu_items(
        self, hotel_id: UUID, *, include_unavailable: bool = False
    ) -> list[HotelMenuItemRecord]:
        query = select(HotelMenuItemRecord).where(HotelMenuItemRecord.hotel_id == hotel_id)
        if not include_unavailable:
            query = query.where(HotelMenuItemRecord.is_available.is_(True))
        rows = await self.session.scalars(
            query.order_by(HotelMenuItemRecord.category, HotelMenuItemRecord.name)
        )
        return list(rows)

    async def menu_matches(self, hotel_id: UUID, text: str) -> list[HotelMenuItemRecord]:
        """Return active menu items explicitly named by the caller.

        Matching is intentionally local and conservative. The LLM may help
        understand a noisy utterance, but it cannot invent an item that the
        hotel has not configured.
        """
        normalized = " ".join(re.findall(r"[a-z0-9]+", text.casefold()))
        if not normalized:
            return []
        matches: list[HotelMenuItemRecord] = []
        for item in await self.menu_items(hotel_id):
            candidates = [item.name]
            if item.aliases:
                try:
                    aliases = json.loads(item.aliases)
                except (TypeError, ValueError, json.JSONDecodeError):
                    aliases = [part.strip() for part in item.aliases.split(",")]
                if isinstance(aliases, list):
                    candidates.extend(str(alias) for alias in aliases)
            if any(
                " ".join(re.findall(r"[a-z0-9]+", candidate.casefold())) in normalized
                for candidate in candidates
                if candidate.strip()
            ):
                matches.append(item)
        return matches

    async def start_conversation(
        self, hotel_id: UUID, caller_phone: str
    ) -> HotelConversationRecord:
        normalized = normalize_phone_number(caller_phone)
        guest = await self.session.scalar(
            select(HotelGuestRecord).where(
                HotelGuestRecord.hotel_id == hotel_id,
                HotelGuestRecord.phone == normalized,
            )
        )
        reservation = None
        room_number = None
        if guest:
            reservation = await self.session.scalar(
                select(HotelReservationRecord)
                .where(
                    HotelReservationRecord.hotel_id == hotel_id,
                    HotelReservationRecord.guest_id == guest.id,
                    HotelReservationRecord.status.in_(["confirmed", "checked_in"]),
                )
                .order_by(HotelReservationRecord.check_in.desc())
            )
            if reservation and reservation.room_id:
                room = await self.session.get(HotelRoomRecord, reservation.room_id)
                room_number = room.room_number if room else None
        conversation = HotelConversationRecord(
            hotel_id=hotel_id,
            caller_phone=normalized,
            # A directory match alone is not proof that this caller is the
            # guest from a previous call. Only a verified active reservation
            # may prefill identity; otherwise the current caller must state it.
            caller_name=guest.full_name if guest and reservation else None,
            room_number=room_number,
            guest_id=guest.id if guest else None,
            reservation_id=reservation.id if reservation else None,
            verification_status="verified_stay" if reservation else "unverified",
            state="ready",
        )
        self.session.add(conversation)
        await self.session.flush()
        return conversation

    async def add_turn(
        self, conversation_id: UUID, speaker: str, text: str
    ) -> HotelConversationTurnRecord:
        turn = HotelConversationTurnRecord(
            conversation_id=conversation_id, speaker=speaker, text=text
        )
        self.session.add(turn)
        await self.session.flush()
        return turn

    async def latest_assistant_turn(self, conversation_id: UUID) -> str | None:
        return await self.session.scalar(
            select(HotelConversationTurnRecord.text)
            .where(
                HotelConversationTurnRecord.conversation_id == conversation_id,
                HotelConversationTurnRecord.speaker == "assistant",
            )
            .order_by(HotelConversationTurnRecord.created_at.desc())
            .limit(1)
        )

    async def conversation(self, conversation_id: UUID) -> HotelConversationRecord | None:
        return await self.session.get(HotelConversationRecord, conversation_id)

    async def update_guest_context(
        self,
        conversation: HotelConversationRecord,
        *,
        full_name: str | None = None,
        room_number: str | None = None,
    ) -> HotelGuestRecord | None:
        """Persist explicit updates to the caller-provided hotel context.

        A caller can be unknown to the PMS at the start of a call. We still
        create the lightweight guest directory record once they provide a name
        so a correction is not only held in the in-memory voice turn. Room
        numbers remain conversation-scoped because the guest directory does
        not own a stay assignment.
        """
        guest = None
        if full_name:
            if conversation.guest_id:
                guest = await self.session.get(HotelGuestRecord, conversation.guest_id)
            if guest is None:
                guest = await self.session.scalar(
                    select(HotelGuestRecord).where(
                        HotelGuestRecord.hotel_id == conversation.hotel_id,
                        HotelGuestRecord.phone == conversation.caller_phone,
                    )
                )
            if guest is None:
                guest = HotelGuestRecord(
                    hotel_id=conversation.hotel_id,
                    full_name=full_name,
                    phone=conversation.caller_phone,
                )
                self.session.add(guest)
                await self.session.flush()
            else:
                guest.full_name = full_name
            conversation.guest_id = guest.id
            conversation.caller_name = full_name
        if room_number:
            conversation.room_number = room_number
        return guest

    async def update_guest_name(
        self, conversation: HotelConversationRecord, full_name: str
    ) -> HotelGuestRecord | None:
        """Backward-compatible wrapper for callers that only update a name."""
        return await self.update_guest_context(conversation, full_name=full_name)

    async def create_request(
        self,
        *,
        conversation: HotelConversationRecord,
        category: str,
        description: str,
        priority: str,
        room_number: str | None,
        department_id: UUID | None,
    ) -> HotelRequestRecord:
        request = HotelRequestRecord(
            hotel_id=conversation.hotel_id,
            conversation_id=conversation.id,
            guest_id=conversation.guest_id,
            reservation_id=conversation.reservation_id,
            category=category,
            priority=priority,
            description=description,
            room_number=room_number,
            department_id=department_id,
        )
        self.session.add(request)
        await self.session.flush()
        self.session.add(
            HotelRequestActionRecord(
                request_id=request.id,
                actor="voice_agent",
                action_type="created",
                detail=f"Created {category} request from inbound conversation.",
            )
        )
        return request

    async def department(self, hotel_id: UUID, key: str) -> HotelDepartmentRecord | None:
        return await self.session.scalar(
            select(HotelDepartmentRecord).where(
                HotelDepartmentRecord.hotel_id == hotel_id,
                HotelDepartmentRecord.key == key,
                HotelDepartmentRecord.is_active.is_(True),
            )
        )

    async def latest_request(self, conversation_id: UUID) -> HotelRequestRecord | None:
        return await self.session.scalar(
            select(HotelRequestRecord)
            .where(HotelRequestRecord.conversation_id == conversation_id)
            .order_by(HotelRequestRecord.created_at.desc())
        )

    async def request_by_id(
        self, conversation_id: UUID, request_id: UUID
    ) -> HotelRequestRecord | None:
        return await self.session.scalar(
            select(HotelRequestRecord).where(
                HotelRequestRecord.id == request_id,
                HotelRequestRecord.conversation_id == conversation_id,
            )
        )

    async def request_history(
        self, conversation_id: UUID, *, limit: int = 5
    ) -> list[HotelRequestRecord]:
        rows = await self.session.scalars(
            select(HotelRequestRecord)
            .where(HotelRequestRecord.conversation_id == conversation_id)
            .order_by(HotelRequestRecord.created_at.desc())
            .limit(limit)
        )
        return list(rows)

    async def list_requests(self, hotel_id: UUID) -> list[HotelRequestRecord]:
        rows = await self.session.scalars(
            select(HotelRequestRecord)
            .where(HotelRequestRecord.hotel_id == hotel_id)
            .order_by(HotelRequestRecord.created_at.desc())
        )
        return list(rows)

    async def list_conversations(self, hotel_id: UUID) -> list[HotelConversationRecord]:
        rows = await self.session.scalars(
            select(HotelConversationRecord)
            .where(HotelConversationRecord.hotel_id == hotel_id)
            .order_by(HotelConversationRecord.created_at.desc())
        )
        return list(rows)

    async def list_reservations(self, hotel_id: UUID) -> list[HotelReservationRecord]:
        rows = await self.session.scalars(
            select(HotelReservationRecord)
            .where(HotelReservationRecord.hotel_id == hotel_id)
            .order_by(HotelReservationRecord.check_in.desc())
        )
        return list(rows)

    async def available_rooms(
        self, hotel_id: UUID, room_type: str | None = None
    ) -> list[HotelRoomRecord]:
        query = select(HotelRoomRecord).where(
            HotelRoomRecord.hotel_id == hotel_id,
            HotelRoomRecord.status == "available",
        )
        if room_type:
            query = query.where(HotelRoomRecord.room_type == room_type)
        return list(await self.session.scalars(query.order_by(HotelRoomRecord.room_number)))

    async def get_reservation(
        self, hotel_id: UUID, reservation_id: UUID
    ) -> HotelReservationRecord | None:
        return await self.session.scalar(
            select(HotelReservationRecord).where(
                HotelReservationRecord.hotel_id == hotel_id,
                HotelReservationRecord.id == reservation_id,
            )
        )

    async def apply_request_action(
        self, request: HotelRequestRecord, actor: str, action: str, detail: str
    ) -> None:
        self.session.add(
            HotelRequestActionRecord(
                request_id=request.id,
                actor=actor,
                action_type=action,
                detail=detail,
            )
        )
