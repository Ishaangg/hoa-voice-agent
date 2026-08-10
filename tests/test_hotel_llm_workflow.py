import json

from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.seed import (
    DEMO_SOLD_OUT_CHECK_IN,
    DEMO_SOLD_OUT_CHECK_OUT,
    HOTEL_ID,
    seed_demo_hotel,
)
from hoa_voice_agent.services.hotel_audit import HotelConversationAuditLog
from hoa_voice_agent.services.hotel_booking_offers import HotelBookingOfferService
from hoa_voice_agent.services.hotel_booking_plan import BookingPlan
from hoa_voice_agent.services.hotel_conversation import HotelConversationService
from hoa_voice_agent.services.hotel_turn_interpreter import HotelTurnInterpretation


async def _mark_no_couples_answered(service: HotelConversationService, conversation_id) -> None:
    """Keep legacy workflow tests focused on their existing behavior."""

    conversation = await service.store.conversation(conversation_id)
    assert conversation is not None
    conversation.booking_plan_json = BookingPlan(
        party_composition_known=True,
    ).to_json()
    await service.session.commit()


class FakeHotelInterpreter:
    async def interpret(self, **kwargs):
        return HotelTurnInterpretation(
            intent="maintenance",
            action="create_request",
            service_category="maintenance",
            service_description="Bathroom fixture has stopped producing hot water",
            room_number="204",
            confidence=0.98,
        )


class ToolUsingHotelInterpreter:
    async def interpret(self, **kwargs):
        result = await kwargs["tool_executor"](
            "get_room_details", {"room_type": "standard"}
        )
        return HotelTurnInterpretation(
            intent="faq",
            action="answer",
            response=f"There are {len(result['rooms'])} standard rooms in the inventory.",
            confidence=0.99,
        )


class ControllerRoutedRecommendationInterpreter:
    controller_owns_tools = True

    def __init__(self) -> None:
        self.received_tool_executor = False

    async def interpret(self, **kwargs):
        self.received_tool_executor = "tool_executor" in kwargs
        return HotelTurnInterpretation(
            intent="booking",
            action="answer",
            dialogue_act="compare",
            command="request_recommendation",
            response="I will check the best room option.",
            confidence=0.99,
        )


class ControllerRoutedCatalogueInterpreter:
    controller_owns_tools = True

    def __init__(self) -> None:
        self.received_tool_executor = False

    async def interpret(self, **kwargs):
        self.received_tool_executor = "tool_executor" in kwargs
        return HotelTurnInterpretation(
            intent="booking",
            action="answer",
            dialogue_act="ask",
            command="ask_about_state",
            command_target="catalogue",
            response="I will list the room types.",
            confidence=0.99,
        )


class SearchThenFormatFailureInterpreter:
    """A tool lookup succeeds but the provider returns no parseable summary."""

    async def interpret(self, **kwargs):
        await kwargs["tool_executor"](
            "search_rooms",
            {
                "check_in": "2026-08-14",
                "check_out": "2026-08-19",
                "guest_count": 5,
                "guest_count_evidence": "5 guests",
            },
        )
        return None


class BookingOptionsInterpreter:
    async def interpret(self, **kwargs):
        result = await kwargs["tool_executor"]("list_room_types", {})
        return HotelTurnInterpretation(
            intent="booking",
            action="ask_slot",
            response=(
                "The available room types are "
                f"{', '.join(result['room_types'])}. Which would you like to consider?"
            ),
            booking_check_in="2030-05-01",
            booking_check_out="2030-05-02",
            booking_guest_count=2,
            booking_nights=1,
            confidence=0.99,
        )


class InventedGuestCountInterpreter:
    async def interpret(self, **kwargs):
        return HotelTurnInterpretation(
            intent="booking",
            action="ask_slot",
            response="Kitne guests honge?",
            booking_check_in="2030-08-14",
            booking_check_out="2030-08-17",
            # This is deliberately not stated by the caller.
            booking_guest_count=1,
            confidence=0.99,
        )


class EvidenceGuestCountInterpreter:
    async def interpret(self, **kwargs):
        return HotelTurnInterpretation(
            intent="booking",
            action="ask_slot",
            response="Aap chaar guests ke liye booking chahte hain.",
            booking_check_in="2030-08-14",
            booking_check_out="2030-08-17",
            booking_guest_count=4,
            booking_guest_count_evidence="चार लोगों के लिए",
            confidence=0.99,
        )


class BookingFactCorrectionInterpreter:
    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, **kwargs):
        self.calls += 1
        text = kwargs["text"].casefold()
        if self.calls == 1:
            return HotelTurnInterpretation(
                intent="booking",
                action="ask_slot",
                response="I have the dates and two guests.",
                booking_check_in="2030-08-14",
                booking_check_out="2030-08-17",
                booking_guest_count=2,
                booking_guest_count_evidence="two guests",
                confidence=0.99,
            )
        if "six" in text:
            return HotelTurnInterpretation(
                intent="booking",
                action="clarify",
                dialogue_act="correct",
                response="I heard six guests.",
                booking_guest_count=6,
                booking_guest_count_evidence="six guests",
                confidence=0.99,
            )
        return HotelTurnInterpretation(
            intent="confirmation",
            action="confirm",
            dialogue_act="accept",
            confidence=0.99,
        )


class NoisyGuestCountInterpreter:
    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return HotelTurnInterpretation(
                intent="booking",
                action="clarify",
                dialogue_act="inform",
                booking_guest_count=2,
                # The model understands the noisy STT phrase semantically, but
                # its evidence is not a verbatim transcript span.
                booking_guest_count_evidence="do logon ke liye",
                confidence=0.82,
            )
        return HotelTurnInterpretation(
            intent="confirmation",
            action="confirm",
            dialogue_act="accept",
            confidence=0.99,
        )


class ReadOnlyLookupCannotCreateRequestInterpreter:
    async def interpret(self, **kwargs):
        result = await kwargs["tool_executor"]("get_open_requests", {"status": "open"})
        return HotelTurnInterpretation(
            intent="maintenance",
            action="create_request",
            service_category="maintenance",
            service_description="This must not be created from a read-only lookup.",
            response=f"You have {len(result['requests'])} open requests.",
            read_only_tools=("get_open_requests",),
            confidence=0.99,
        )


class SoldOutStandardRoomInterpreter:
    async def interpret(self, **kwargs):
        result = await kwargs["tool_executor"](
            "search_rooms",
            {
                "check_in": DEMO_SOLD_OUT_CHECK_IN.isoformat(),
                "check_out": DEMO_SOLD_OUT_CHECK_OUT.isoformat(),
                "guest_count": 2,
                "guest_count_evidence": "2 guests",
                "room_type": "standard",
            },
        )
        assert result["rooms"] == []
        assert result["alternative_room_types"] == [
            {
                "room_type": "suite",
                "available_room_count": 2,
                "from_nightly_rate_cents": 31900,
                "max_occupancy": 4,
            }
        ]
        return HotelTurnInterpretation(
            intent="booking",
            action="ask_slot",
            # The application must not depend on the model remembering to say
            # that the requested type is sold out.
            response="I can see suites for those dates. Would you like one?",
            booking_check_in=DEMO_SOLD_OUT_CHECK_IN.isoformat(),
            booking_check_out=DEMO_SOLD_OUT_CHECK_OUT.isoformat(),
            booking_guest_count=2,
            booking_guest_count_evidence="2 guests",
            # The model may mention an offered suite. It must not replace the
            # caller's standard-room preference until the caller accepts it.
            booking_room_preference="suite",
            read_only_tools=("search_rooms",),
            confidence=0.99,
        )


class ExactRoomSelectionInterpreter:
    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, **kwargs):
        self.calls += 1
        if self.calls > 1:
            return HotelTurnInterpretation(
                intent="confirmation",
                action="confirm",
                dialogue_act="accept",
                confidence=0.99,
            )
        await kwargs["tool_executor"](
            "search_rooms",
            {
                "check_in": "2026-09-20",
                "check_out": "2026-09-22",
                "guest_count": 2,
                "guest_count_evidence": "2 guests",
                "room_type": "standard",
            },
        )
        return HotelTurnInterpretation(
            intent="booking",
            action="answer",
            response="I found a room.",
            booking_check_in="2026-09-20",
            booking_check_out="2026-09-22",
            booking_guest_count=2,
            booking_guest_count_evidence="2 guests",
            booking_plan_json=(
                '{"room_allocation":[{"room_type":"standard","quantity":1}]}'
            ),
            read_only_tools=("search_rooms",),
            confidence=0.99,
        )


class UnderCapacityRoomSelectionInterpreter:
    """The caller explicitly asks for fewer beds than their party needs."""

    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, **kwargs):
        self.calls += 1
        if self.calls > 1:
            return HotelTurnInterpretation(
                intent="confirmation",
                action="confirm",
                dialogue_act="accept",
                confidence=0.99,
            )
        await kwargs["tool_executor"](
            "search_rooms",
            {
                "check_in": "2026-08-17",
                "check_out": "2026-08-19",
                "guest_count": 5,
                "guest_count_evidence": "5 guests",
                "room_type": "standard",
            },
        )
        return HotelTurnInterpretation(
            intent="booking",
            action="answer",
            response="I will check two standard rooms.",
            booking_check_in="2026-08-17",
            booking_check_out="2026-08-19",
            booking_guest_count=5,
            booking_guest_count_evidence="5 guests",
            booking_plan_json=(
                '{"room_allocation":[{"room_type":"standard","quantity":2}]}'
            ),
            read_only_tools=("search_rooms",),
            confidence=0.99,
        )


class MultiRoomRecommendationInterpreter:
    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, **kwargs):
        self.calls += 1
        if self.calls > 1:
            return HotelTurnInterpretation(
                intent="confirmation",
                action="confirm",
                dialogue_act="accept",
                confidence=0.99,
            )
        result = await kwargs["tool_executor"](
            "search_rooms",
            {
                "check_in": "2026-08-14",
                "check_out": "2026-08-19",
                "guest_count": 5,
                "guest_count_evidence": "5 guests",
            },
        )
        assert result["rooms"] == []
        assert result["multi_room_recommendations"]
        return HotelTurnInterpretation(
            intent="booking",
            action="answer",
            response="This response must be replaced by the verified PMS recommendation.",
            booking_check_in="2026-08-14",
            booking_check_out="2026-08-19",
            booking_guest_count=5,
            booking_guest_count_evidence="5 guests",
            read_only_tools=("search_rooms",),
            confidence=0.99,
        )


class BookingCompletionRegressionInterpreter:
    """Model misses short approval and repeats persisted slots on name turn."""

    def __init__(self, *, approve_semantically: bool) -> None:
        self.calls = 0
        self.approve_semantically = approve_semantically

    async def interpret(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            await kwargs["tool_executor"](
                "search_rooms",
                {
                    "check_in": "2026-08-13",
                    "check_out": "2026-08-15",
                    "guest_count": 2,
                    "guest_count_evidence": "2 guests",
                    "room_type": "standard",
                },
            )
            return HotelTurnInterpretation(
                intent="booking",
                action="answer",
                response="I found an option.",
                booking_check_in="2026-08-13",
                booking_check_out="2026-08-15",
                booking_guest_count=2,
                booking_guest_count_evidence="2 guests",
                booking_plan_json=(
                    '{"room_allocation":[{"room_type":"standard","quantity":1}]}'
                ),
                read_only_tools=("search_rooms",),
                confidence=0.99,
            )
        if self.calls == 2:
            return HotelTurnInterpretation(
                intent="confirmation" if self.approve_semantically else "booking",
                action="confirm" if self.approve_semantically else "answer",
                dialogue_act="accept" if self.approve_semantically else "inform",
                response="I could not understand that.",
                confidence=0.99,
            )
        return HotelTurnInterpretation(
            intent="booking",
            action="ask_slot",
            dialogue_act="inform",
            name="Vishal",
            response="Aapke naam par booking details note kar li hain.",
            # These are persisted context repeated by the model, not new
            # values stated by the caller on the name turn.
            booking_check_in="2026-08-13",
            booking_check_out="2026-08-15",
            booking_nights=2,
            booking_guest_count=2,
            booking_guest_count_evidence=None,
            confidence=0.99,
        )


async def test_hindi_short_approval_recovers_when_interpreter_misses_acceptance() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=BookingCompletionRegressionInterpreter(
                    approve_semantically=False
                ),
                response_language="hinglish",
            )
            conversation_id, _ = await service.start("+15550107771")

            await service.handle_turn(
                conversation_id,
                "I need one standard room from August 13 to August 15 for 2 guests.",
            )
            accepted = await service.handle_turn(conversation_id, "जी")

            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert "hold kar diya hai" in accepted.say.lower()
            assert conversation.booking_hold_reservation_id is not None
    finally:
        await database.dispose()


def test_confirmation_polarity_handles_hindi_and_conflicting_model_labels() -> None:
    unknown = HotelTurnInterpretation()
    model_says_accept = HotelTurnInterpretation(
        intent="confirmation",
        action="confirm",
        dialogue_act="accept",
    )
    model_says_reject = HotelTurnInterpretation(
        intent="booking",
        action="none",
        dialogue_act="reject",
    )

    assert HotelConversationService._turn_accepts(
        unknown, "ji", confirmation_context=True
    )
    assert HotelConversationService._turn_accepts(
        unknown, "जी", confirmation_context=True
    )
    assert HotelConversationService._turn_rejects(
        unknown, "nahi ji", confirmation_context=True
    )
    assert HotelConversationService._turn_rejects(
        unknown, "नहीं जी", confirmation_context=True
    )
    # Explicit caller polarity wins if the model emitted the opposite act.
    assert not HotelConversationService._turn_accepts(
        model_says_accept, "no", confirmation_context=True
    )
    assert HotelConversationService._turn_rejects(
        model_says_reject, "ji", confirmation_context=True
    ) is False


async def test_hindi_short_rejection_releases_the_active_offer() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=BookingCompletionRegressionInterpreter(
                    approve_semantically=False
                ),
                response_language="hinglish",
            )
            conversation_id, _ = await service.start("+15550107773")

            await service.handle_turn(
                conversation_id,
                "I need one standard room from August 13 to August 15 for 2 guests.",
            )
            declined = await service.handle_turn(conversation_id, "नहीं जी")

            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert declined.intent == "booking_cancelled"
            assert "remove" in declined.say.lower()
            assert conversation.booking_offer_id is None
            assert conversation.state == "ready"
    finally:
        await database.dispose()


async def test_name_turn_confirms_hold_when_model_repeats_persisted_booking_slots() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=BookingCompletionRegressionInterpreter(
                    approve_semantically=True
                ),
                response_language="hinglish",
            )
            conversation_id, _ = await service.start("+15550107772")

            await service.handle_turn(
                conversation_id,
                "I need one standard room from August 13 to August 15 for 2 guests.",
            )
            await service.handle_turn(conversation_id, "जी")
            confirmed = await service.handle_turn(
                conversation_id,
                "Mera naam Vishal hai.",
            )

            assert confirmed.intent == "booking_confirmed"
            assert "confirmation number" in confirmed.say.lower()
    finally:
        await database.dispose()


class ModelCopiesRecommendationIntoPlanInterpreter:
    """Reproduce a model treating PMS advice as caller-selected layout."""

    async def interpret(self, **kwargs):
        result = await kwargs["tool_executor"](
            "search_rooms",
            {
                "check_in": "2026-08-17",
                "check_out": "2026-08-19",
                "guest_count": 5,
                "guest_count_evidence": "5 guests",
            },
        )
        assert result["multi_room_recommendations"]
        return HotelTurnInterpretation(
            intent="booking",
            action="answer",
            response="I found an option.",
            booking_check_in="2026-08-17",
            booking_check_out="2026-08-19",
            booking_guest_count=5,
            booking_guest_count_evidence="5 guests",
            # This is the malformed output observed in the live audit log:
            # one five-person group incorrectly marked as separate across two
            # rooms, instead of preserving the PMS 4+1 recommendation.
            booking_plan_json=(
                '{"allocation_mode":"separate",'
                '"party_groups":[{"label":"guests",'
                '"people_per_group":5,"count":1}],'
                '"room_allocation":[{"room_type":"suite","quantity":1},'
                '{"room_type":"standard","quantity":1}]}'
            ),
            read_only_tools=("search_rooms",),
            confidence=0.99,
        )


class InventedRoomQuantityInterpreter:
    async def interpret(self, **kwargs):
        return HotelTurnInterpretation(
            intent="booking",
            action="answer",
            dialogue_act="choose",
            explicit_selection=True,
            response="I found a standard room option.",
            booking_check_in="2030-08-14",
            booking_check_out="2030-08-17",
            booking_guest_count=2,
            # The caller said "standard rooms" but did not say how many.
            booking_plan_json=(
                '{"room_allocation":[{"room_type":"standard","quantity":1}]}'
            ),
            confidence=0.99,
        )


class ContextThenNoMoreInterpreter:
    """The selection turn must use persisted state, not call the LLM again."""

    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            await _kwargs["tool_executor"](
                "search_rooms",
                {
                    "check_in": "2026-09-20",
                    "check_out": "2026-09-22",
                    "guest_count": 2,
                    "guest_count_evidence": "two guests",
                },
            )
            return HotelTurnInterpretation(
                intent="booking",
                action="ask_slot",
                response="Which room type would you like to consider?",
                booking_check_in="2026-09-20",
                booking_check_out="2026-09-22",
                booking_guest_count=2,
                booking_guest_count_evidence="two guests",
                confidence=0.99,
            )
        raise AssertionError("A short room-type selection must not need another LLM call")


class ComparisonAfterSearchInterpreter:
    """The second turn asks for alternatives instead of accepting the first result."""

    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            await kwargs["tool_executor"](
                "search_rooms",
                {
                    "check_in": DEMO_SOLD_OUT_CHECK_IN.isoformat(),
                    "check_out": DEMO_SOLD_OUT_CHECK_OUT.isoformat(),
                    "guest_count": 2,
                    "guest_count_evidence": "2 guests",
                },
            )
            return HotelTurnInterpretation(
                intent="booking",
                action="answer",
                dialogue_act="inform",
                response="I found two suites for those dates.",
                booking_check_in=DEMO_SOLD_OUT_CHECK_IN.isoformat(),
                booking_check_out=DEMO_SOLD_OUT_CHECK_OUT.isoformat(),
                booking_guest_count=2,
                booking_guest_count_evidence="2 guests",
                read_only_tools=("search_rooms",),
            )
        return HotelTurnInterpretation(
            intent="booking",
            action="answer",
            dialogue_act="compare",
            response="I will check other options.",
        )


async def test_llm_interpretation_routes_unusual_language_without_keyword_matching() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=FakeHotelInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109998")

            response = await service.handle_turn(
                conversation_id,
                "The bathroom fixture stopped producing warm water.",
            )

            assert response.intent == "request_created"
            request = await service.store.latest_request(conversation_id)
            assert request is not None
            assert request.category == "maintenance"
            assert request.room_number == "204"
    finally:
        await database.dispose()


async def test_hotel_service_passes_read_only_tools_to_the_interpreter() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=ToolUsingHotelInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109997")

            response = await service.handle_turn(conversation_id, "Tell me about standard rooms.")

            assert response.intent == "faq"
            assert "standard sleeps up to 2 guests" in response.say
    finally:
        await database.dispose()


async def test_controller_routes_recommendation_without_giving_interpreter_tool_executor() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            interpreter = ControllerRoutedRecommendationInterpreter()
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=interpreter,
            )
            conversation_id, _ = await service.start("+15550109995")
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            conversation.booking_check_in = DEMO_SOLD_OUT_CHECK_IN
            conversation.booking_check_out = DEMO_SOLD_OUT_CHECK_OUT
            conversation.booking_guest_count = 2
            conversation.booking_plan_json = BookingPlan(
                party_composition_known=True,
            ).to_json()
            conversation.pending_category = "booking"
            conversation.state = "collecting_booking_allocation"
            await service.session.commit()

            response = await service.handle_turn(
                conversation_id,
                "What do you recommend?",
            )

            assert interpreter.received_tool_executor is False
            assert response.intent == "booking"
            assert "suite" in response.say.lower()
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert '"tool": "search_rooms"' in (
                conversation.booking_last_search_result or ""
            )
    finally:
        await database.dispose()


async def test_controller_routes_catalogue_question_to_verified_read_without_tool_executor(
) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            interpreter = ControllerRoutedCatalogueInterpreter()
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=interpreter,
            )
            conversation_id, _ = await service.start("+15550109994")

            response = await service.handle_turn(
                conversation_id,
                "What room types do you have?",
            )

            assert interpreter.received_tool_executor is False
            assert response.intent == "booking"
            assert "room types" in response.say.lower()
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert '"tool": "list_room_types"' in (
                conversation.booking_last_search_result or ""
            )
    finally:
        await database.dispose()


async def test_verified_pms_response_survives_post_tool_llm_format_failure() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=SearchThenFormatFailureInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109990")
            await _mark_no_couples_answered(service, conversation_id)

            response = await service.handle_turn(
                conversation_id,
                "I need a room from August 14 to August 19 for 5 guests.",
            )

            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert response.intent == "booking"
            assert "No single room fits 5 guests" in response.say
            assert conversation.booking_offer_id is not None
            assert conversation.state == "awaiting_booking_confirmation"
    finally:
        await database.dispose()


async def test_hotel_service_audits_pms_tool_result_with_conversation_id(tmp_path) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            path = tmp_path / "hotel-conversation-audit.jsonl"
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=ToolUsingHotelInterpreter(),
                audit_log=HotelConversationAuditLog(path),
            )
            conversation_id, _ = await service.start("+15550109997")

            await service.handle_turn(conversation_id, "Tell me about standard rooms.")

            events = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            pms_event = next(event for event in events if event["event"] == "pms_tool_result")
            assert pms_event["conversation_id"] == str(conversation_id)
            assert pms_event["data"]["tool"] == "get_room_details"
            assert pms_event["data"]["arguments"] == {"room_type": "standard"}
    finally:
        await database.dispose()


async def test_booking_tool_response_is_not_replaced_by_the_legacy_controller() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=BookingOptionsInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109996")

            response = await service.handle_turn(
                conversation_id,
                "What room options do you have?",
            )

            assert response.intent == "booking"
            assert response.say == (
                "For your group, I can see these room types: King, Standard, Suite. "
                "Would you like a recommendation, or details about one of them?"
            )
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert "room_types" in (conversation.booking_last_search_result or "")
    finally:
        await database.dispose()


async def test_model_guest_count_does_not_update_state_without_caller_evidence() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=InventedGuestCountInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109990")

            await service.handle_turn(
                conversation_id,
                "Mujhe 14 August se 17 August tak room chahiye.",
            )

            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert conversation.booking_check_in is not None
            assert conversation.booking_check_out is not None
            assert conversation.booking_guest_count is None
    finally:
        await database.dispose()


async def test_model_guest_count_with_transcript_evidence_updates_state() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=EvidenceGuestCountInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109991")

            await service.handle_turn(
                conversation_id,
                "Mujhe चार लोगों के लिए room chahiye, 14 August se 17 August tak.",
            )

            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert conversation.booking_guest_count == 4
    finally:
        await database.dispose()


async def test_booking_fact_correction_requires_confirmation_and_supersedes_old_value() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=BookingFactCorrectionInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109992")

            await service.handle_turn(
                conversation_id,
                "I need a room from August 14 to August 17 for two guests.",
            )
            correction = await service.handle_turn(
                conversation_id,
                "Actually six guests will be staying.",
            )
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert "from 2 guests to 6 guests" in correction.say
            assert conversation.booking_guest_count == 2
            assert '"status": "pending_confirmation"' in (
                conversation.booking_facts_json or ""
            )

            confirmed = await service.handle_turn(conversation_id, "Yes, update it.")
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert "updated guest count to 6 guests" in confirmed.say
            assert conversation.booking_guest_count == 6
            assert '"status": "superseded"' in (conversation.booking_facts_json or "")
    finally:
        await database.dispose()


async def test_noisy_hinglish_guest_count_is_confirmed_before_becoming_active() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=NoisyGuestCountInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109993")

            clarification = await service.handle_turn(
                conversation_id,
                "Mujhe dono ke liye hotel book karna hai.",
            )
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert "2 guests" in clarification.say
            assert conversation.booking_guest_count is None

            await service.handle_turn(conversation_id, "Haan, use that.")
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert conversation.booking_guest_count == 2
    finally:
        await database.dispose()


async def test_read_only_tool_result_cannot_trigger_a_request_controller() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=ReadOnlyLookupCannotCreateRequestInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109995")

            response = await service.handle_turn(
                conversation_id,
                "Do I have any open maintenance requests?",
            )

            assert response.intent == "maintenance"
            assert response.say == "You have 0 open requests."
            assert await service.store.latest_request(conversation_id) is None
    finally:
        await database.dispose()


async def test_sold_out_room_type_offers_only_the_dated_available_alternative() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=SoldOutStandardRoomInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109994")
            await _mark_no_couples_answered(service, conversation_id)

            response = await service.handle_turn(
                conversation_id,
                "I need a standard room from September 17 to September 19 for 2 guests.",
            )

            assert response.intent == "booking"
            assert response.say == (
                "Standard rooms are sold out for September 17 through September 19. "
                "2 suite rooms are available. Would you like to consider a suite or "
                "check different dates?"
            )
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert conversation.booking_room_preference == "standard"
            assert "alternative_room_types" in (conversation.booking_last_search_result or "")
            events = await HotelBookingOfferService(session, HOTEL_ID).tool_events(conversation_id)
            assert events[-1]["tool_name"] == "search_rooms"
            assert events[-1]["arguments"]["room_type"] == "standard"
            assert events[-1]["result"]["requested_room_type_status"] == "sold_out"
    finally:
        await database.dispose()


async def test_comparison_turn_refreshes_inventory_instead_of_repeating_saved_answer() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            interpreter = ComparisonAfterSearchInterpreter()
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=interpreter,
            )
            conversation_id, _ = await service.start("+15550109995")
            await _mark_no_couples_answered(service, conversation_id)

            await service.handle_turn(
                conversation_id,
                "I need a room from September 17 to September 19 for 2 guests.",
            )
            response = await service.handle_turn(
                conversation_id,
                "Do you have any other options?",
            )

            assert response.intent == "booking_options"
            assert "I do not see another room type" in response.say
            assert "Would you like me to check different dates?" in response.say
            assert interpreter.calls == 2
            events = await HotelBookingOfferService(session, HOTEL_ID).tool_events(
                conversation_id
            )
            assert [event["tool_name"] for event in events].count("search_rooms") == 2
    finally:
        await database.dispose()


async def test_llm_selected_exact_layout_becomes_an_offer_before_a_hold() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=ExactRoomSelectionInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109993")

            offered = await service.handle_turn(
                conversation_id,
                "I need one standard room from September 20 to September 22 for 2 guests.",
            )
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert offered.intent == "booking"
            assert "requested layout" in offered.say.lower()
            assert conversation.booking_offer_id is not None
            assert conversation.booking_hold_reservation_id is None

            accepted = await service.handle_turn(conversation_id, "Okay ma'am, kar do please.")
            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert "placed that exact option on hold" in accepted.say.lower()
            assert conversation.booking_offer_id is not None
            assert conversation.booking_hold_reservation_id is not None
    finally:
        await database.dispose()


async def test_under_capacity_layout_explains_limit_and_presents_an_alternative() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=UnderCapacityRoomSelectionInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109991")

            response = await service.handle_turn(
                conversation_id,
                "I want 2 standard rooms from August 17 to August 19 for 5 guests.",
            )

            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert "2 standard rooms can accommodate up to 4 guests" in response.say
            assert "I recommend 3 standard rooms" in response.say
            assert "Would you like me to hold that exact layout?" in response.say
            assert conversation.booking_offer_id is not None
            assert conversation.booking_hold_reservation_id is None

            accepted = await service.handle_turn(conversation_id, "Yes, please.")
            assert "placed that exact option on hold" in accepted.say.lower()
    finally:
        await database.dispose()


async def test_no_single_room_fit_recommends_a_multi_room_offer_before_a_hold() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=MultiRoomRecommendationInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109992")
            await _mark_no_couples_answered(service, conversation_id)

            recommended = await service.handle_turn(
                conversation_id,
                "I need a room from August 14 to August 19 for 5 guests.",
            )

            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert recommended.intent == "booking"
            assert "No single room fits 5 guests" in recommended.say
            assert "I have 3 standard rooms and 2 suite rooms available" in recommended.say
            assert "I recommend 1 standard room and 1 suite room" in recommended.say
            assert "Would you like me to hold that exact layout?" in recommended.say
            assert conversation.booking_offer_id is not None
            assert conversation.booking_hold_reservation_id is None
            assert (
                BookingPlan.from_json(
                    conversation.booking_plan_json
                ).party_composition_known
                is True
            )

            accepted = await service.handle_turn(conversation_id, "Yes, please.")

            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert "placed that exact option on hold" in accepted.say.lower()
            assert conversation.booking_hold_reservation_id is not None
    finally:
        await database.dispose()


async def test_model_cannot_replace_pms_recommendation_with_an_unspoken_layout() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=ModelCopiesRecommendationIntoPlanInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109993")
            await _mark_no_couples_answered(service, conversation_id)

            response = await service.handle_turn(
                conversation_id,
                "What do you recommend for August 17 to August 19 for 5 guests?",
            )

            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert response.intent == "booking"
            assert "I recommend 1 standard room and 1 suite room" in response.say
            assert "I won't substitute another layout" not in response.say
            assert (
                BookingPlan.from_json(
                    conversation.booking_plan_json
                ).party_composition_known
                is True
            )
            assert conversation.booking_offer_id is not None
    finally:
        await database.dispose()


async def test_model_cannot_invent_room_quantity_in_booking_plan() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            service = HotelConversationService(
                session=session,
                hotel_id=HOTEL_ID,
                interpreter=InventedRoomQuantityInterpreter(),
            )
            conversation_id, _ = await service.start("+15550109989")

            await service.handle_turn(
                conversation_id,
                "I need standard rooms from August 14 to August 17 for two guests.",
            )

            conversation = await service.store.conversation(conversation_id)
            assert conversation is not None
            assert conversation.booking_plan_json is None
    finally:
        await database.dispose()


async def test_short_room_type_selection_uses_the_active_booking_context() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        async with database.session_factory() as session:
            await seed_demo_hotel(session)
            for index, phrase in enumerate(("Standard", "A standard", "Standard room type")):
                interpreter = ContextThenNoMoreInterpreter()
                service = HotelConversationService(
                    session=session,
                    hotel_id=HOTEL_ID,
                    interpreter=interpreter,
                )
                conversation_id, _ = await service.start(f"+1555010778{index}")
                await _mark_no_couples_answered(service, conversation_id)

                initial = await service.handle_turn(
                    conversation_id,
                    "I need a room for two guests.",
                )
                assert initial.say == (
                    "For September 20 through September 22, Standard and Suite "
                    "rooms are available for your stay. Which room type would you "
                    "like to consider?"
                )
                assert "3 standard" not in initial.say.lower()
                assert "2 suite" not in initial.say.lower()
                response = await service.handle_turn(conversation_id, phrase)

                assert interpreter.calls == 1
                assert response.intent == "booking"
                assert response.say == (
                    "Standard rooms are available for September 20 through September 22. "
                    "Would you like to consider that option?"
                )
                conversation = await service.store.conversation(conversation_id)
                assert conversation is not None
                assert conversation.booking_room_preference == "standard"
    finally:
        await database.dispose()
