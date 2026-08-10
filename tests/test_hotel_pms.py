from datetime import date

from hoa_voice_agent.adapters.hotel_pms import HotelPMSAdapter
from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.seed import (
    DEMO_SOLD_OUT_CHECK_IN,
    DEMO_SOLD_OUT_CHECK_OUT,
    HOTEL_ID,
    seed_demo_hotel,
)
from hoa_voice_agent.services.hotel_booking_plan import (
    BookingPlan,
    PartyGroup,
    RoomTypeRequest,
)


async def test_seeded_september_standard_sellout_exposes_two_suite_alternatives() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            availability = await HotelPMSAdapter(
                session, HOTEL_ID
            ).search_booking_availability(
                check_in=DEMO_SOLD_OUT_CHECK_IN,
                check_out=DEMO_SOLD_OUT_CHECK_OUT,
                guest_count=2,
                requested_room_type="standard",
            )

            assert availability["requested_room_type"] == "standard"
            assert availability["requested_room_type_status"] == "sold_out"
            assert availability["check_in"] == "2026-09-17"
            assert availability["check_out"] == "2026-09-19"
            assert availability["rooms"] == []
            assert availability["alternative_room_types"] == [
                {
                    "room_type": "suite",
                    "available_room_count": 2,
                    "from_nightly_rate_cents": 31900,
                    "max_occupancy": 4,
                }
            ]
    finally:
        await database.dispose()


async def test_room_type_phrase_is_normalized_before_dated_availability_search() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            availability = await HotelPMSAdapter(
                session, HOTEL_ID
            ).search_booking_availability(
                check_in=DEMO_SOLD_OUT_CHECK_OUT,
                check_out=DEMO_SOLD_OUT_CHECK_OUT.replace(day=21),
                guest_count=2,
                requested_room_type="standard room",
            )

            assert availability["requested_room_type"] == "standard"
            assert availability["requested_room_type_status"] == "available"
            assert len(availability["rooms"]) == 3
            assert availability["alternative_room_types"] == []
    finally:
        await database.dispose()


async def test_search_recommends_a_multi_room_layout_when_no_single_room_fits() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            availability = await HotelPMSAdapter(
                session, HOTEL_ID
            ).search_booking_availability(
                check_in=date(2026, 8, 14),
                check_out=date(2026, 8, 19),
                guest_count=5,
            )

            assert availability["rooms"] == []
            assert availability["available_room_types"] == [
                {
                    "room_type": "standard",
                    "available_room_count": 3,
                    "from_nightly_rate_cents": 15900,
                    "max_occupancy": 2,
                },
                {
                    "room_type": "suite",
                    "available_room_count": 2,
                    "from_nightly_rate_cents": 31900,
                    "max_occupancy": 4,
                },
            ]
            assert availability["multi_room_recommendations"][0] == {
                "allocation": [
                    {"room_type": "standard", "quantity": 1},
                    {"room_type": "suite", "quantity": 1},
                ],
                "room_lines": [
                    {"room_type": "suite", "guests": 4},
                    {"room_type": "standard", "guests": 1},
                ],
                "room_count": 2,
                "total_capacity": 6,
                "nightly_total_cents": 47800,
            }
    finally:
        await database.dispose()


async def test_multi_room_recommendation_preserves_a_stated_room_type_when_possible() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            availability = await HotelPMSAdapter(
                session, HOTEL_ID
            ).search_booking_availability(
                check_in=date(2026, 8, 14),
                check_out=date(2026, 8, 19),
                guest_count=5,
                requested_room_type="standard",
            )

            assert availability["requested_room_type_status"] == "does_not_fit_party"
            assert availability["multi_room_recommendations"][0]["allocation"] == [
                {"room_type": "standard", "quantity": 3}
            ]
    finally:
        await database.dispose()


async def test_couple_aware_recommendation_keeps_three_couples_in_three_rooms() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            plan = BookingPlan(
                party_groups=[PartyGroup("couple", 2, 3)],
                party_composition_known=True,
                allow_room_sharing=False,
            )
            availability = await HotelPMSAdapter(
                session, HOTEL_ID
            ).search_booking_availability(
                check_in=date(2026, 8, 14),
                check_out=date(2026, 8, 19),
                guest_count=6,
                booking_plan=plan,
            )

            recommendations = availability["multi_room_recommendations"]
            assert recommendations
            assert recommendations[0]["allocation"] == [
                {"room_type": "standard", "quantity": 3}
            ]
            assert recommendations[0]["room_lines"] == [
                {"room_type": "standard", "guests": 2},
                {"room_type": "standard", "guests": 2},
                {"room_type": "standard", "guests": 2},
            ]
    finally:
        await database.dispose()


async def test_under_capacity_requested_layout_returns_checked_alternatives() -> None:
    """A missing party split must not hide a physical capacity shortfall."""

    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            result = await HotelPMSAdapter(session, HOTEL_ID).search_booking_plan(
                BookingPlan(
                    allocation=[RoomTypeRequest(room_type="standard", quantity=2)],
                ),
                guest_count=5,
                check_in=date(2026, 8, 17),
                check_out=date(2026, 8, 19),
            )

            assert result["status"] == "unavailable"
            assert result["failure_code"] == "insufficient_capacity"
            assert result["requested_total_capacity"] == 4
            assert result["guest_count"] == 5
            assert result["alternative_recommendations"][0]["allocation"] == [
                {"room_type": "standard", "quantity": 3}
            ]
    finally:
        await database.dispose()
