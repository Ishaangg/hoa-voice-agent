from datetime import date

import pytest

from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.seed import HOTEL_ID, seed_demo_hotel
from hoa_voice_agent.services.hotel_booking_offers import HotelBookingOfferService
from hoa_voice_agent.services.hotel_booking_plan import BookingPlan, RoomTypeRequest
from hoa_voice_agent.services.hotel_conversation import HotelConversationService


def _standard_room_plan(*, version: int = 1) -> BookingPlan:
    return BookingPlan(
        version=version,
        allocation=[RoomTypeRequest(room_type="standard", quantity=1)],
    )


async def test_new_booking_draft_supersedes_the_previous_presented_offer() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            conversation_id, _ = await HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                allow_legacy_language_rules=True,
            ).start("+15550108881")
            conversation = await HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                allow_legacy_language_rules=True,
            ).store.conversation(conversation_id)
            assert conversation is not None

            offers = HotelBookingOfferService(session, HOTEL_ID)
            first = await offers.present_offer(
                conversation=conversation,
                plan=_standard_room_plan(version=1),
                guest_count=2,
                check_in=date(2026, 9, 20),
                check_out=date(2026, 9, 22),
            )
            replacement = await offers.present_offer(
                conversation=conversation,
                plan=_standard_room_plan(version=2),
                guest_count=2,
                check_in=date(2026, 9, 21),
                check_out=date(2026, 9, 23),
            )

            assert first["status"] == "presented"
            assert replacement["status"] == "presented"
            assert replacement["offer_id"] != first["offer_id"]
            assert (await offers.get_offer(first["offer_id"]))["status"] == "superseded"
            assert (await offers.get_offer(replacement["offer_id"]))["status"] == "presented"
    finally:
        await database.dispose()


async def test_accepting_an_offer_creates_a_revalidated_hold() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                allow_legacy_language_rules=True,
            )
            conversation_id, _ = await service.start("+15550108882")
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None

            offers = HotelBookingOfferService(session, HOTEL_ID)
            offer = await offers.present_offer(
                conversation=conversation,
                plan=_standard_room_plan(),
                guest_count=2,
                check_in=date(2026, 9, 20),
                check_out=date(2026, 9, 22),
            )
            held = await offers.accept_offer(conversation=conversation, offer_id=offer["offer_id"])

            assert held["status"] == "held"
            assert held["hold_reservation_id"]
            assert held["rooms"] == offer["rooms"]
            assert (await offers.get_offer(offer["offer_id"]))["status"] == "held"
    finally:
        await database.dispose()


async def test_accepting_an_expired_or_superseded_offer_is_rejected() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                allow_legacy_language_rules=True,
            )
            conversation_id, _ = await service.start("+15550108883")
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None

            offers = HotelBookingOfferService(session, HOTEL_ID)
            original = await offers.present_offer(
                conversation=conversation,
                plan=_standard_room_plan(version=1),
                guest_count=2,
                check_in=date(2026, 9, 20),
                check_out=date(2026, 9, 22),
            )
            await offers.present_offer(
                conversation=conversation,
                plan=_standard_room_plan(version=2),
                guest_count=2,
                check_in=date(2026, 9, 21),
                check_out=date(2026, 9, 23),
            )

            with pytest.raises(ValueError, match="no longer current"):
                await offers.accept_offer(conversation=conversation, offer_id=original["offer_id"])
    finally:
        await database.dispose()
