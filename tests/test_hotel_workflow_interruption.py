from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.seed import HOTEL_ID, seed_demo_hotel
from hoa_voice_agent.services.hotel_conversation import HotelConversationService


async def test_new_request_defers_and_resumes_an_unfinished_booking() -> None:
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
            conversation_id, _ = await service.start("+15550109995")
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            conversation.room_number = "204"
            conversation.pending_category = "booking"
            conversation.pending_description = "A room for two guests tomorrow night."
            conversation.state = "awaiting_booking_confirmation"
            await session.commit()

            first = await service.handle_turn(
                conversation_id, "My air conditioning is not working."
            )
            assert first.intent == "clarification"
            assert conversation.pending_category == "maintenance"
            assert conversation.deferred_category == "booking"
            assert conversation.deferred_state == "awaiting_booking_confirmation"

            completed = await service.handle_turn(conversation_id, "Please send maintenance.")
            assert completed.intent == "request_created"
            assert "still have your room booking" in completed.say.lower()
            assert conversation.pending_category is None
            assert conversation.state == "awaiting_deferred_resume"
            assert conversation.deferred_category == "booking"

            blocked = await service.handle_turn(
                conversation_id, "I also need extra towels."
            )
            assert blocked.intent == "clarification"
            assert "continue or discard" in blocked.say.lower()
            assert conversation.deferred_category == "booking"

            resumed = await service.handle_turn(conversation_id, "Yes, please.")
            assert resumed.intent == "workflow_resumed"
            assert conversation.pending_category == "booking"
            assert conversation.state == "awaiting_booking_confirmation"
            assert conversation.deferred_category is None
    finally:
        await database.dispose()


async def test_third_competing_request_does_not_drop_the_suspended_workflow() -> None:
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
            conversation_id, _ = await service.start("+15550109996")
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            conversation.pending_category = "maintenance"
            conversation.pending_description = "The air conditioning is not working."
            conversation.state = "awaiting_issue_details"
            conversation.deferred_category = "booking"
            conversation.deferred_description = "A room for two guests tomorrow night."
            conversation.deferred_state = "awaiting_booking_confirmation"
            await session.commit()

            response = await service.handle_turn(
                conversation_id, "I also need extra towels."
            )

            assert response.intent == "clarification"
            assert "finish or cancel" in response.say.lower()
            assert conversation.pending_category == "maintenance"
            assert conversation.deferred_category == "booking"
    finally:
        await database.dispose()
