from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.seed import HOTEL_ID, seed_demo_hotel
from hoa_voice_agent.services.hotel_conversation import HotelConversationService
from hoa_voice_agent.services.hotel_turn_interpreter import HotelTurnInterpretation


class CompositionFlowInterpreter:
    def __init__(self, plan_json: str) -> None:
        self.plan_json = plan_json
        self.calls = 0

    async def interpret(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return HotelTurnInterpretation(
                intent="booking",
                action="ask_slot",
                dialogue_act="inform",
                booking_check_in="2026-08-14",
                booking_check_out="2026-08-19",
                booking_guest_count=6,
                booking_guest_count_evidence="six guests",
            )
        return HotelTurnInterpretation(
            intent="booking",
            action="answer",
            dialogue_act="inform",
            booking_plan_json=self.plan_json,
        )


class SingleCoupleInterpreter:
    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return HotelTurnInterpretation(
                intent="booking",
                action="ask_slot",
                dialogue_act="inform",
                booking_guest_count=3,
                booking_guest_count_evidence="three guests",
            )
        return HotelTurnInterpretation(
            intent="booking",
            action="ask_slot",
            dialogue_act="inform",
            response=(
                "Theek hai. Baaki guest couple ke saath share karega, "
                "ya alag room chahiye?"
            ),
            follow_up_question="Share karega ya alag room chahiye?",
            booking_plan_json=(
                '{"party_composition_known":true,"party_groups":['
                '{"label":"couple","people_per_group":2,"count":1}]}'
            ),
        )


async def test_six_guest_flow_asks_couples_then_recommends_three_couple_rooms() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=CompositionFlowInterpreter(
                    '{"party_composition_known":true,"party_groups":['
                    '{"label":"couple","people_per_group":2,"count":3}]}'
                ),
            )
            conversation_id, _ = await service.start("+15550108881")

            follow_up = await service.handle_turn(
                conversation_id,
                "I need a room from August 14 to August 19 for six guests.",
            )
            assert "couples" in follow_up.say
            assert "one room" not in follow_up.say

            recommendation = await service.handle_turn(
                conversation_id,
                "Teen couples hain.",
            )

            assert recommendation.intent == "booking_options"
            assert "3 standard rooms" in recommendation.say
            assert "2 guests" in recommendation.say
    finally:
        await database.dispose()


async def test_no_couples_uses_the_existing_aggregate_recommendation_path() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=CompositionFlowInterpreter(
                    '{"party_composition_known":true,"party_groups":[]}'
                ),
            )
            conversation_id, _ = await service.start("+15550108882")

            await service.handle_turn(
                conversation_id,
                "I need a room from August 14 to August 19 for six guests.",
            )
            recommendation = await service.handle_turn(
                conversation_id,
                "No couples, just the group.",
            )

            assert recommendation.intent == "booking_options"
            assert "suite" in recommendation.say.lower()
            assert "standard" in recommendation.say.lower()
    finally:
        await database.dispose()


async def test_one_couple_does_not_ask_remaining_guest_to_choose_sharing() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=SingleCoupleInterpreter(),
                response_language="hinglish",
            )
            conversation_id, _ = await service.start("+15550108883")

            await service.handle_turn(
                conversation_id,
                "I need a room for three guests.",
            )
            response = await service.handle_turn(
                conversation_id,
                "There is one couple.",
            )

            assert "share" not in response.say.lower()
            assert "party" not in response.say.lower()
            assert "check-in" in response.say.lower()
    finally:
        await database.dispose()
