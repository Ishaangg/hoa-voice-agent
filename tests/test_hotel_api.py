from pathlib import Path

import httpx
import pytest

from hoa_voice_agent.api import create_app
from hoa_voice_agent.config import Settings
from hoa_voice_agent.persistence.seed import HOTEL_ID


@pytest.fixture
async def hotel_client(tmp_path: Path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'hotel.db'}",
        sms_mode="fake",
        groq_api_key=None,
        hotel_llm_api_key=None,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def hotel_login(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/hotel/auth/login",
        json={"email": "hotel.manager@evergreen.demo", "password": "demo-pass"},
    )
    assert response.status_code == 200


async def test_hotel_workspace_isolated_and_request_lifecycle(
    hotel_client: httpx.AsyncClient,
) -> None:
    response = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550101111"},
    )
    assert response.status_code == 201
    conversation_id = response.json()["conversation_id"]
    turn = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My air conditioning is not working in room 204."},
    )
    assert turn.status_code == 200
    assert turn.json()["intent"] == "clarification"
    assert turn.json()["request_id"] is None
    turn = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "The air conditioning is still not working; please send maintenance."},
    )
    assert turn.json()["intent"] == "request_created"
    assert turn.json()["request_id"]

    # Hotel data is not visible through HOA console routes before hotel login.
    assert (await hotel_client.get("/api/v1/console/hotel/dashboard")).status_code == 401
    await hotel_login(hotel_client)
    dashboard = await hotel_client.get("/api/v1/console/hotel/dashboard")
    assert dashboard.status_code == 200
    assert dashboard.json()["kpis"]["open_requests"] == 1
    assert dashboard.json()["priority_queue"][0]["status"] == "open"
    request_rows = await hotel_client.get("/api/v1/console/hotel/requests")
    assert request_rows.json()[0]["notification_status"] == "sent"

    knowledge = await hotel_client.post(
        "/api/v1/console/hotel/knowledge",
        json={
            "category": "amenity",
            "topic": "parking",
            "answer": "Valet parking is available at the front entrance.",
            "source_reference": "Demo hotel guide",
            "approval_status": "approved",
        },
    )
    assert knowledge.status_code == 200
    assert (await hotel_client.get("/api/v1/console/hotel/knowledge")).status_code == 200

    request_id = dashboard.json()["priority_queue"][0]["id"]
    acknowledged = await hotel_client.post(
        f"/api/v1/console/hotel/requests/{request_id}/acknowledge"
    )
    assert acknowledged.json()["status"] == "acknowledged"
    assigned = await hotel_client.post(
        f"/api/v1/console/hotel/requests/{request_id}/assign",
        params={"assigned_to": "Engineering"},
    )
    assert assigned.json()["status"] == "assigned"
    resolved = await hotel_client.post(
        f"/api/v1/console/hotel/requests/{request_id}/resolve",
        json={"resolution_note": "Engineering restored cooling and guest confirmed comfort."},
    )
    assert resolved.json()["status"] == "resolved"


async def test_hotel_demo_pms_reservation_is_explicitly_demo(
    hotel_client: httpx.AsyncClient,
) -> None:
    await hotel_login(hotel_client)
    reservation = await hotel_client.post(
        "/api/v1/console/hotel/reservations",
        json={
            "hotel_id": str(HOTEL_ID),
            "guest_name": "New Demo Guest",
            "phone": "+15550102222",
            "check_in": "2030-05-01",
            "check_out": "2030-05-03",
            "room_type": "standard",
            "guest_count": 1,
        },
    )
    assert reservation.status_code == 201
    assert reservation.json()["payment_status"] == "demo_confirmed"
    reservation_id = reservation.json()["reservation_id"]
    modified = await hotel_client.patch(
        f"/api/v1/console/hotel/reservations/{reservation_id}",
        json={"check_in": "2030-05-02", "check_out": "2030-05-04"},
    )
    assert modified.status_code == 200
    cancelled = await hotel_client.post(
        f"/api/v1/console/hotel/reservations/{reservation_id}/cancel"
    )
    assert cancelled.json()["status"] == "cancelled"


async def test_hotel_inventory_is_date_aware_and_checkout_day_can_turn_over(
    hotel_client: httpx.AsyncClient,
) -> None:
    """Three dated reservations consume three standards only while overlapping."""

    await hotel_login(hotel_client)
    for index in range(3):
        created = await hotel_client.post(
            "/api/v1/console/hotel/reservations",
            json={
                "hotel_id": str(HOTEL_ID),
                "guest_name": f"Dated Guest {index}",
                "phone": f"+1555010300{index}",
                "check_in": "2032-05-01",
                "check_out": "2032-05-03",
                "room_type": "standard",
                "guest_count": 1,
            },
        )
        assert created.status_code == 201

    overlapping = await hotel_client.get(
        "/api/v1/console/hotel/availability",
        params={"room_type": "standard", "check_in": "2032-05-01", "check_out": "2032-05-03"},
    )
    assert overlapping.json() == []

    turnover = await hotel_client.get(
        "/api/v1/console/hotel/availability",
        params={"room_type": "standard", "check_in": "2032-05-03", "check_out": "2032-05-04"},
    )
    assert len(turnover.json()) == 3


async def test_voice_booking_confirms_a_multi_room_hold_with_room_lines(
    hotel_client: httpx.AsyncClient,
) -> None:
    """A multi-room quote remains a hold until the caller accepts and names it."""

    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550103444"},
    )
    conversation_id = started.json()["conversation_id"]
    for text in (
        "I need a room tomorrow for 5 guests, for one night.",
        "We are two couples and one single.",
        "Separate rooms, please.",
    ):
        await hotel_client.post(
            f"/api/v1/hotel/conversations/{conversation_id}/turns", json={"text": text}
        )
    quote = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Can I do 3 standard rooms?"},
    )
    assert "hold it for five minutes" in quote.json()["say"].lower()

    accepted = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns", json={"text": "Yes, please."}
    )
    assert "what name" in accepted.json()["say"].lower()
    confirmed = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Ada Lovelace."},
    )
    assert confirmed.json()["intent"] == "booking_confirmed"
    assert "confirmation number" in confirmed.json()["say"].lower()
    assert "booked from" in confirmed.json()["say"].lower()
    assert "whatsapp" in confirmed.json()["say"].lower()
    assert "is there something that i can help you with" in confirmed.json()["say"].lower()

    closed = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "No, thank you."},
    )
    assert closed.json()["intent"] == "conversation_closed"
    assert "thank you" in closed.json()["say"].lower()

    await hotel_login(hotel_client)
    reservations = await hotel_client.get("/api/v1/console/hotel/reservations")
    ada = next(row for row in reservations.json() if row["guest_name"] == "Ada Lovelace")
    assert ada["status"] == "confirmed"
    assert len(ada["rooms"]) == 3
    assert sum(room["guest_count"] for room in ada["rooms"]) == 5


async def test_voice_hold_blocks_competing_quote_then_releases_on_cancellation(
    hotel_client: httpx.AsyncClient,
) -> None:
    """Quotes do not reserve inventory; accepting one rechecks before the hold."""

    async def quote_three_standards(phone: str) -> tuple[str, str]:
        started = await hotel_client.post(
            "/api/v1/hotel/conversations",
            json={"hotel_id": str(HOTEL_ID), "caller_phone": phone},
        )
        conversation_id = started.json()["conversation_id"]
        for text in (
            "I need a room tomorrow for 5 guests, for one night.",
            "We are two couples and one single.",
            "Separate rooms, please.",
        ):
            await hotel_client.post(
                f"/api/v1/hotel/conversations/{conversation_id}/turns", json={"text": text}
            )
        response = await hotel_client.post(
            f"/api/v1/hotel/conversations/{conversation_id}/turns",
            json={"text": "Can I do 3 standard rooms?"},
        )
        return conversation_id, response.json()["say"]

    first_id, first_quote = await quote_three_standards("+15550103501")
    assert "hold it for five minutes" in first_quote.lower()

    second_id, second_quote = await quote_three_standards("+15550103502")
    assert "hold it for five minutes" in second_quote.lower()

    accepted = await hotel_client.post(
        f"/api/v1/hotel/conversations/{first_id}/turns", json={"text": "Yes, please."}
    )
    assert "what name" in accepted.json()["say"].lower()

    blocked = await hotel_client.post(
        f"/api/v1/hotel/conversations/{second_id}/turns", json={"text": "Yes, please."}
    )
    assert "no longer available" in blocked.json()["say"].lower()

    released = await hotel_client.post(
        f"/api/v1/hotel/conversations/{first_id}/turns", json={"text": "Cancel the booking."}
    )
    assert released.json()["intent"] == "booking_cancelled"

    retried = await hotel_client.post(
        f"/api/v1/hotel/conversations/{second_id}/turns",
        json={"text": "Can I do 3 standard rooms?"},
    )
    assert "hold it for five minutes" in retried.json()["say"].lower()


async def test_booking_question_about_an_unavailable_room_type_keeps_booking_context(
    hotel_client: httpx.AsyncClient,
) -> None:
    """A side question must not be mistaken for the next booking slot."""

    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550102223"},
    )
    conversation_id = started.json()["conversation_id"]

    booking = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Can you find a room for 2 people tomorrow night?"},
    )
    assert booking.json()["intent"] == "booking"

    detail = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "What does the deluxe room include?"},
    )

    assert detail.json()["intent"] == "booking_detail"
    assert "couldn't find a deluxe room" in detail.json()["say"].lower()

    options = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "What other options do you have?"},
    )
    assert options.json()["intent"] == "booking_options"
    assert "couples" in options.json()["say"].lower()


async def test_booking_slots_override_check_in_time_faq_and_persist_across_turns(
    hotel_client: httpx.AsyncClient,
) -> None:
    """A stated stay date is booking state, never a check-in-hours question."""

    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550102224"},
    )
    conversation_id = started.json()["conversation_id"]

    await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "I wanna book a room."},
    )
    supplied = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={
            "text": (
                "Check-in date will be tomorrow. Checkout will be a day after tomorrow. "
                "And I want a room for 5."
            )
        },
    )

    assert supplied.json()["intent"] == "booking"
    assert "3 pm" not in supplied.json()["say"].lower()
    assert "5 guests" in supplied.json()["say"].lower()

    recommendation = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "What do you recommend?"},
    )
    assert recommendation.json()["intent"] == "booking_options"
    assert "suite since it can accommodate up to four" not in recommendation.json()["say"].lower()

    follow_up = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Got it."},
    )
    assert "check-in and check-out dates" not in follow_up.json()["say"].lower()
    assert "how many guests" not in follow_up.json()["say"].lower()


async def test_booking_keeps_tomorrow_guest_wording_and_one_night_across_turns(
    hotel_client: httpx.AsyncClient,
) -> None:
    """Natural voice phrasing must fill state before the next prompt is chosen."""

    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550102225"},
    )
    conversation_id = started.json()["conversation_id"]

    tomorrow = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Can you check the rooms for tomorrow?"},
    )
    assert "check-in and check-out dates" not in tomorrow.json()["say"].lower()

    guest_count = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "The guest staying will be 5. What do you recommend?"},
    )
    assert "how many guests" not in guest_count.json()["say"].lower()

    one_night = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Only 1 night. I prefer whatever you recommend."},
    )
    assert "5 guests" in one_night.json()["say"].lower()
    assert "check-in and check-out dates" not in one_night.json()["say"].lower()


async def test_booking_collects_party_layout_and_never_substitutes_exact_room_request(
    hotel_client: httpx.AsyncClient,
) -> None:
    """A caller's allocation is a hard constraint, not a recommendation input."""

    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550102226"},
    )
    conversation_id = started.json()["conversation_id"]

    await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "I need a room tomorrow for 5 guests, for one night."},
    )
    composition = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "We are two couples and one single."},
    )
    assert "recommend 3 standard rooms" in composition.json()["say"].lower()

    room_split = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Separate rooms, please."},
    )
    assert "recommend 3 standard rooms" in room_split.json()["say"].lower()

    exact_request = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Can I do 3 standard rooms?"},
    )
    say = exact_request.json()["say"].lower()
    assert "requested layout" in say
    assert "hold it for five minutes" in say
    assert "suite" not in say


async def test_booking_prices_a_single_exact_room_with_its_guest_occupancy(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550102227"},
    )
    conversation_id = started.json()["conversation_id"]

    await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "I need a room tomorrow for 2 guests, for one night."},
    )
    offer = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "One standard room, please."},
    )

    say = offer.json()["say"].lower()
    assert "requested layout" in say
    assert "2 guests in standard at $159.00" in offer.json()["say"]
    assert "$159.00" in say


async def test_hotel_voice_intake_understands_spoken_room_digits(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550103333"},
    )
    conversation_id = started.json()["conversation_id"]
    captured = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Ishawn, and my room number is 4 4."},
    )
    assert "room 44" in captured.json()["say"]
    room = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "4 to 4."},
    )
    assert "room 424" in room.json()["say"]
    name = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "What is my name?"},
    )
    assert name.json()["say"] == "Your name is Ishawn."


async def test_hotel_voice_intake_understands_natural_room_number_answer(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550108888"},
    )
    conversation_id = started.json()["conversation_id"]
    name = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Vicky."},
    )
    assert "room number" in name.json()["say"]

    room = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "It's four four."},
    )
    assert "room 44" in room.json()["say"]


async def test_hotel_voice_intake_persists_name_correction_in_call_context(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550104444"},
    )
    conversation_id = started.json()["conversation_id"]

    captured = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Michelle, and my room number is 7 3 8."},
    )
    assert "Michelle" in captured.json()["say"]

    correction = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Not Michelle. It's Ishaan."},
    )
    assert correction.json()["intent"] == "identity"
    assert "updated your name to Ishaan" in correction.json()["say"]

    name = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "What is my name?"},
    )
    assert name.json()["say"] == "Your name is Ishaan."


async def test_hotel_voice_intake_persists_name_variants_and_corrections(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550106666"},
    )
    conversation_id = started.json()["conversation_id"]

    captured = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Name's Vishan. And my room number is 9 0 2."},
    )
    assert "Vishan" in captured.json()["say"]

    name = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "What is my name?"},
    )
    assert name.json()["say"] == "Your name is Vishan."

    correction = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Name is Ishaan."},
    )
    assert correction.json()["intent"] == "identity"
    assert "updated your name to Ishaan" in correction.json()["say"]

    corrected_name = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "What is my name?"},
    )
    assert corrected_name.json()["say"] == "Your name is Ishaan."


async def test_hotel_voice_intake_updates_any_explicit_identity_field(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550105555"},
    )
    conversation_id = started.json()["conversation_id"]

    await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Priya, and my room number is 410."},
    )
    room_correction = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Actually, my room number is 412."},
    )
    assert room_correction.json()["intent"] == "identity"
    assert "now in room 412" in room_correction.json()["say"]

    name_correction = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Anika."},
    )
    assert name_correction.json()["intent"] == "identity"
    assert "updated your name to Anika" in name_correction.json()["say"]

    room = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "What is my room number?"},
    )
    assert room.json()["say"] == "Your room number is 412."


async def test_hotel_room_service_uses_known_room_and_routes_to_food_beverage(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550106666"},
    )
    conversation_id = started.json()["conversation_id"]
    await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Vicky, and my room number is 14."},
    )

    confirmation = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Can I have noodles and Manchurian in my room?"},
    )
    assert confirmation.json()["intent"] == "confirmation"
    assert "noodles and Manchurian" in confirmation.json()["say"]

    request = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Yes, please."},
    )
    assert request.json()["intent"] == "request_created"
    assert "room 14" in request.json()["say"]

    await hotel_login(hotel_client)
    requests = await hotel_client.get("/api/v1/console/hotel/requests")
    latest = requests.json()[0]
    assert latest["category"] == "room_service"
    assert latest["department"] == "Food and Beverage"
    assert latest["room_number"] == "14"
    assert latest["notification_status"] == "sent"


async def test_hotel_generic_food_request_asks_for_order_details(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550107777"},
    )
    conversation_id = started.json()["conversation_id"]
    await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Vicky, and my room number is 40."},
    )

    clarification = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "I need food in my room."},
    )
    assert clarification.json()["intent"] == "clarification"
    assert clarification.json()["say"] == "What would you like to order?"

    confirmation = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Noodles and Manchurian."},
    )
    assert confirmation.json()["intent"] == "confirmation"
    assert "room 40" in confirmation.json()["say"]

    cancelled = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "No, cancel that."},
    )
    assert cancelled.json()["intent"] == "cancelled"
    await hotel_login(hotel_client)
    requests = await hotel_client.get("/api/v1/console/hotel/requests")
    assert requests.json() == []


async def test_hotel_menu_question_lists_items_instead_of_treating_menu_as_an_item(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550107778"},
    )
    conversation_id = started.json()["conversation_id"]
    await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Vicky, and my room number is 40."},
    )

    menu = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "What things are on the menu?"},
    )

    assert menu.json()["intent"] == "clarification"
    assert "couldn’t find" not in menu.json()["say"].lower()
    assert "Coffee" in menu.json()["say"]


async def test_hotel_cancellation_closes_request_instead_of_creating_another(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550107779"},
    )
    conversation_id = started.json()["conversation_id"]
    await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Vicky, and my room number is 40."},
    )
    created = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "The bathroom pipe is leaking."},
    )
    assert created.json()["intent"] == "request_created"

    cancelled = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Cancel the request. I don't need it right now."},
    )
    assert cancelled.json()["intent"] == "cancelled"

    await hotel_login(hotel_client)
    requests = await hotel_client.get("/api/v1/console/hotel/requests")
    assert len(requests.json()) == 1
    assert requests.json()[0]["status"] == "cancelled"


async def test_hotel_conversational_i_am_phrase_does_not_overwrite_name(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550107780"},
    )
    conversation_id = started.json()["conversation_id"]
    await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Vicky, and my room number is 40."},
    )

    response = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "I am not feeling that much hungry."},
    )
    assert "updated your name" not in response.json()["say"].lower()

    name = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "What is my name?"},
    )
    assert name.json()["say"] == "Your name is Vicky."


async def test_hotel_new_call_does_not_reuse_unverified_directory_name(
    hotel_client: httpx.AsyncClient,
) -> None:
    phone = "+15550109991"
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": phone},
    )
    first_id = started.json()["conversation_id"]
    await hotel_client.post(
        f"/api/v1/hotel/conversations/{first_id}/turns",
        json={"text": "My name is Vicky."},
    )

    second = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": phone},
    )
    assert second.json()["say"] == (
        "Thank you for calling Evergreen Harbor Hotel. How can I help you today?"
    )


async def test_hotel_identity_prompt_does_not_block_general_questions(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550109993"},
    )
    conversation_id = started.json()["conversation_id"]

    identity_question = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "What is my name?"},
    )
    assert identity_question.json()["say"] == (
        "I do not have your name yet. What name should I use?"
    )

    general_question = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Do you have something in the database?"},
    )
    assert general_question.json()["intent"] == "conversation"
    assert "What is your name" not in general_question.json()["say"]


async def test_hotel_room_service_collects_room_before_menu_item(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550109994"},
    )
    conversation_id = started.json()["conversation_id"]

    room_prompt = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "I need food in my room."},
    )
    assert room_prompt.json()["intent"] == "identity"
    assert "room" in room_prompt.json()["say"].lower()

    details_prompt = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Room 414."},
    )
    assert details_prompt.json()["intent"] == "clarification"
    assert details_prompt.json()["say"] == "Thanks. What would you like to order?"

    confirmation = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "Maggi noodles."},
    )
    assert confirmation.json()["intent"] == "confirmation"
    assert "maggi noodles" in confirmation.json()["say"].lower()


async def test_hotel_menu_lookup_confirms_alias_and_menu_api_is_configurable(
    hotel_client: httpx.AsyncClient,
) -> None:
    started = await hotel_client.post(
        "/api/v1/hotel/conversations",
        json={"hotel_id": str(HOTEL_ID), "caller_phone": "+15550109992"},
    )
    conversation_id = started.json()["conversation_id"]
    await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "My name is Ishaan and my room is 14."},
    )
    confirmation = await hotel_client.post(
        f"/api/v1/hotel/conversations/{conversation_id}/turns",
        json={"text": "I would like some Maggie."},
    )
    assert confirmation.json()["intent"] == "confirmation"
    assert "Maggi Noodles" in confirmation.json()["say"]
    assert "$25.00" in confirmation.json()["say"]

    await hotel_login(hotel_client)
    menu = await hotel_client.get("/api/v1/console/hotel/menu")
    assert menu.status_code == 200
    assert any(item["name"] == "Maggi Noodles" for item in menu.json())
    created = await hotel_client.post(
        "/api/v1/console/hotel/menu",
        json={
            "category": "Mains",
            "name": "Paneer Tikka",
            "aliases": ["paneer", "tikka"],
            "price_cents": 2800,
            "is_available": True,
        },
    )
    assert created.status_code == 201
    item_id = created.json()["id"]
    disabled = await hotel_client.patch(
        f"/api/v1/console/hotel/menu/{item_id}",
        json={"is_available": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["is_available"] is False
