import json
from datetime import date

from sqlalchemy import event

from hoa_voice_agent.adapters.hotel_pms import HotelPMSAdapter
from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.seed import HOTEL_ID, seed_demo_hotel
from hoa_voice_agent.services.hotel_booking_plan import BookingPlan
from hoa_voice_agent.services.hotel_catalog import CatalogSnapshotService
from hoa_voice_agent.services.hotel_conversation import HotelConversationService
from hoa_voice_agent.services.hotel_recommendations import (
    BookingCriteria,
    RecommendationEngine,
)
from hoa_voice_agent.services.hotel_turn_interpreter import HotelTurnInterpretation


async def test_catalog_metadata_is_persisted_and_cached() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    CatalogSnapshotService.clear_cache()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = CatalogSnapshotService(session, HOTEL_ID)
            first = await service.get()
            second = await service.get()

            assert first is second
            standard = first.find_room_type("standard")
            suite = first.find_room_type("suite")
            assert standard is not None
            assert suite is not None
            assert "wifi" in standard.amenity_keys
            assert "pool" in suite.amenity_keys
            assert standard.primary_rate_plan is not None
            assert standard.primary_rate_plan.nightly_rate_cents == 15900
    finally:
        await database.dispose()


async def test_catalog_load_uses_fixed_queries_and_cache_hit_reads_nothing() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    CatalogSnapshotService.clear_cache()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            statements: list[str] = []

            def count_statement(*args) -> None:
                statement = str(args[2]).lstrip().upper()
                if statement.startswith("SELECT"):
                    statements.append(statement)

            event.listen(database.engine.sync_engine, "before_cursor_execute", count_statement)
            try:
                service = CatalogSnapshotService(session, HOTEL_ID)
                await service.get()
                loaded_count = len(statements)
                await service.get()
            finally:
                event.remove(
                    database.engine.sync_engine,
                    "before_cursor_execute",
                    count_statement,
                )

            assert 0 < loaded_count <= 6
            assert len(statements) == loaded_count
    finally:
        await database.dispose()


async def test_catalog_metadata_survives_cache_clear_and_new_session(tmp_path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'hotel-catalog.db'}"
    database = Database(database_url)
    await database.create_schema()
    CatalogSnapshotService.clear_cache()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            first = await CatalogSnapshotService(session, HOTEL_ID).get()
            assert first.find_room_type("suite").primary_rate_plan.nightly_rate_cents == 31900
    finally:
        await database.dispose()

    CatalogSnapshotService.clear_cache()
    restarted_database = Database(database_url)
    await restarted_database.create_schema()
    try:
        async with restarted_database.session_factory() as session:
            second = await CatalogSnapshotService(session, HOTEL_ID).get()
            suite = second.find_room_type("suite")
            assert suite is not None
            assert "pool" in suite.amenity_keys
            assert suite.primary_rate_plan is not None
            assert suite.primary_rate_plan.nightly_rate_cents == 31900
    finally:
        await restarted_database.dispose()


async def test_recommendation_engine_filters_hard_requirements_and_budget() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    CatalogSnapshotService.clear_cache()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            snapshot = await CatalogSnapshotService(session, HOTEL_ID).get()
            recommendations = RecommendationEngine().rank(
                snapshot,
                [
                    {"room_type": "standard", "room_id": "standard-1", "nightly_rate_cents": 15900},
                    {"room_type": "suite", "room_id": "suite-1", "nightly_rate_cents": 31900},
                ],
                guest_count=2,
                criteria=BookingCriteria(
                    required_features=("pool",),
                    max_nightly_rate_cents=32000,
                ),
            )

            assert [item["allocation"][0]["room_type"] for item in recommendations] == [
                "suite"
            ]
            assert "swimming pool access" in recommendations[0]["explanation"]

            removed = BookingCriteria(required_features=("pool",)).apply_patch(
                removed_features=["pool"],
                known_features=snapshot.feature_keys,
            )
            assert removed.required_features == ()
            assert removed.revision == 1
    finally:
        await database.dispose()


async def test_pms_search_returns_catalogue_ranked_recommendations() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    CatalogSnapshotService.clear_cache()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            snapshot = await CatalogSnapshotService(session, HOTEL_ID).get()
            result = await HotelPMSAdapter(session, HOTEL_ID).search_booking_availability(
                check_in=date(2026, 8, 14),
                check_out=date(2026, 8, 15),
                guest_count=2,
                criteria=BookingCriteria(required_features=("pool",)),
                catalog=snapshot,
            )

            assert result["recommendations"][0]["allocation"] == [
                {"room_type": "suite", "quantity": 1}
            ]
    finally:
        await database.dispose()


class PoolRecommendationInterpreter:
    async def interpret(self, **kwargs):
        await kwargs["tool_executor"](
            "search_rooms",
            {
                "check_in": "2026-08-14",
                "check_out": "2026-08-15",
                "guest_count": 2,
                "guest_count_evidence": "2 guests",
                "required_features": ["pool"],
                "criteria_evidence": {"required_features": "pool"},
            },
        )
        return HotelTurnInterpretation(
            intent="booking",
            action="answer",
            read_only_tools=("search_rooms",),
            confidence=0.99,
        )


class AmenityQuestionInterpreter:
    async def interpret(self, **kwargs):
        await kwargs["tool_executor"](
            "get_room_details",
            {"room_type": "suite", "response_scope": "feature_match"},
        )
        return HotelTurnInterpretation(
            intent="faq",
            action="answer",
            read_only_tools=("get_room_details",),
            confidence=0.99,
        )


async def test_conversation_presents_ranked_offer_without_dumping_catalogue() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    CatalogSnapshotService.clear_cache()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=PoolRecommendationInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109998")
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            conversation.booking_plan_json = BookingPlan(
                party_composition_known=True,
            ).to_json()
            await session.commit()
            response = await service.handle_turn(
                conversation_id,
                "I need a room for 2 guests with pool access.",
            )
            conversation = await service.store.conversation(conversation_id)

            assert response.intent == "booking"
            assert "Suite" in response.say
            assert "swimming pool access" in response.say
            assert "3 standard rooms" not in response.say
            assert conversation is not None
            assert json.loads(conversation.booking_criteria_json or "{}")[
                "required_features"
            ] == ["pool"]
            assert conversation.booking_offer_id is not None
    finally:
        await database.dispose()


async def test_room_amenity_question_uses_only_the_requested_progressive_scope() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    CatalogSnapshotService.clear_cache()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=AmenityQuestionInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109997")
            response = await service.handle_turn(
                conversation_id,
                "Does the suite have Wi-Fi?",
            )

            assert response.intent == "faq"
            assert "Wi-Fi" in response.say
            assert "sleeps up to" not in response.say
            assert "$" not in response.say
    finally:
        await database.dispose()
