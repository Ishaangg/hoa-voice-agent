from pathlib import Path

import httpx
import pytest

from hoa_voice_agent.api import create_app
from hoa_voice_agent.config import Settings
from hoa_voice_agent.services.conversation import ConversationService


@pytest.fixture
async def client(tmp_path: Path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        nvidia_api_key=None,
        sms_mode="fake",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def start_conversation(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/api/v1/conversations",
        json={"caller_phone": "+15550101111"},
    )
    assert response.status_code == 201
    conversation_id = response.json()["conversation_id"]
    details = await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "My name is Alex and I am in Unit 204."},
    )
    assert details.status_code == 200
    return conversation_id


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_new_caller_starts_with_an_unverified_profile(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/conversations",
        json={"caller_phone": "+15550101111"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["state"] == "awaiting_resident_details"
    assert payload["resident_id"] is None
    assert "name and unit number" in payload["say"]


async def test_unknown_caller_is_asked_for_name_and_unit_before_routine_help(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/conversations",
        json={"caller_phone": "+15550102222"},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["state"] == "awaiting_resident_details"
    assert "name and unit number" in payload["say"]

    details = await client.post(
        f"/api/v1/conversations/{payload['conversation_id']}/turns",
        json={"text": "My name is Jordan Smith and I am in Unit 407."},
    )

    assert details.status_code == 200
    assert details.json()["intent"] == "conversation"
    assert "Jordan Smith in Unit 407" in details.json()["say"]

    repeat_call = await client.post(
        "/api/v1/conversations",
        json={"caller_phone": "+15550102222"},
    )

    assert repeat_call.status_code == 201
    assert repeat_call.json()["state"] == "ready"
    assert "Welcome back, Jordan Smith in Unit 407" in repeat_call.json()["say"]


async def test_caller_can_correct_a_claimed_unit_after_intake(client: httpx.AsyncClient) -> None:
    first_call = await client.post(
        "/api/v1/conversations",
        json={"caller_phone": "+15550102223"},
    )
    conversation_id = first_call.json()["conversation_id"]
    await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "My name is Ishan and my unit number is seven."},
    )

    correction = await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "Actually, I want to change my unit number. It is three zero four."},
    )
    assert "Unit 304" in correction.json()["say"]
    assert "Should I update" in correction.json()["say"]

    confirmation = await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "Yes."},
    )
    assert "updated your unit to Unit 304" in confirmation.json()["say"]

    repeat_call = await client.post(
        "/api/v1/conversations",
        json={"caller_phone": "+15550102223"},
    )
    assert "Welcome back, Ishan in Unit 304" in repeat_call.json()["say"]


async def test_caller_can_use_natural_language_to_correct_profile_details(
    client: httpx.AsyncClient,
) -> None:
    first_call = await client.post(
        "/api/v1/conversations",
        json={"caller_phone": "+15550102224"},
    )
    conversation_id = first_call.json()["conversation_id"]
    await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "My name is Ishan and my unit number is seven."},
    )

    correction = await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "No, that's wrong. I live in 304."},
    )
    assert "Unit 304" in correction.json()["say"]

    await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "Yes"},
    )
    name_correction = await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "Please call me Priya."},
    )
    assert "your name to Priya" in name_correction.json()["say"]


async def test_unknown_caller_emergency_is_not_delayed_for_intake(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/api/v1/conversations",
        json={"caller_phone": "+15550102222"},
    )

    emergency = await client.post(
        f"/api/v1/conversations/{response.json()['conversation_id']}/turns",
        json={"text": "I smell gas in the lobby."},
    )

    assert emergency.status_code == 200
    assert emergency.json()["decision"]["priority"] == "emergency"


def test_spoken_resident_details_do_not_leak_into_name_or_unit() -> None:
    name, unit = ConversationService._extract_caller_details(
        "Uh, my name is Ishan and my unit number is seven three seven."
    )

    assert name == "Ishan"
    assert unit == "Unit 737"


def test_spoken_unit_fillers_are_ignored() -> None:
    name, unit = ConversationService._extract_caller_details(
        "Uh, my name is Ishan and my unit number is uh. three Oh. three."
    )

    assert name == "Ishan"
    assert unit == "Unit 303"


def test_natural_unit_restatement_is_detected_as_a_correction() -> None:
    name, unit = ConversationService._extract_profile_correction(
        "I'm sorry, you missed my unit number is seven. Zero three."
    )

    assert name is None
    assert unit == "Unit 703"


async def test_faq_turn_returns_community_answer(client: httpx.AsyncClient) -> None:
    conversation_id = await start_conversation(client)

    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "What time does the pool close?"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "faq"
    assert "9 PM" in payload["say"]
    assert payload["actions"] == []


async def test_community_knowledge_base_answers_guest_parking_question(
    client: httpx.AsyncClient,
) -> None:
    conversation_id = await start_conversation(client)

    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "Can my guest park overnight?"},
    )

    assert response.status_code == 200
    assert response.json()["intent"] == "faq"
    assert "guest pass" in response.json()["say"]


async def test_active_leak_creates_work_order_sms_and_transfer(
    client: httpx.AsyncClient,
) -> None:
    conversation_id = await start_conversation(client)

    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "Water is leaking from the ceiling in Building A hallway."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "maintenance"
    assert payload["decision"]["priority"] == "emergency"
    assert [action["type"] for action in payload["actions"]] == [
        "work_order_created",
        "sms_queued",
        "transfer_requested",
    ]

    work_orders = await client.get(
        f"/api/v1/conversations/{conversation_id}/work-orders"
    )
    assert work_orders.status_code == 200
    assert len(work_orders.json()) == 1
    assert work_orders.json()[0]["priority"] == "emergency"


async def test_lift_entrapment_is_an_emergency_and_transfers_to_on_call(
    client: httpx.AsyncClient,
) -> None:
    conversation_id = await start_conversation(client)

    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "I am trapped in the lift."},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "maintenance"
    assert payload["decision"]["incident_type"] == "elevator_entrapment"
    assert payload["decision"]["priority"] == "emergency"
    assert [action["type"] for action in payload["actions"]] == [
        "work_order_created",
        "sms_queued",
        "transfer_requested",
    ]
    assert "emergency call button" in payload["say"]


async def test_missing_conversation_returns_404(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/conversations/00000000-0000-0000-0000-000000000000/turns",
        json={"text": "What are the pool hours?"},
    )

    assert response.status_code == 404


async def test_operations_dashboard_exposes_kpis_events_and_transcript(
    client: httpx.AsyncClient,
) -> None:
    conversation_id = await start_conversation(client)
    await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "Water is leaking from the ceiling in the hallway."},
    )

    dashboard = await client.get("/operations")
    assert dashboard.status_code == 200
    assert "Operations Control Room" in dashboard.text

    summary = await client.get("/api/v1/operations/summary")
    assert summary.status_code == 200
    assert summary.json()["calls"] >= 1
    assert summary.json()["calls_today"] >= 1
    assert summary.json()["open_work_orders"] >= 1
    assert summary.json()["active_emergencies"] >= 1
    assert summary.json()["urgent_open_work_orders"] >= 1
    assert summary.json()["emergency_escalations"] >= 1

    transcript = await client.get(
        f"/api/v1/operations/transcripts/{conversation_id}"
    )
    assert [turn["speaker"] for turn in transcript.json()] == [
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
    ]

    events = await client.get("/api/v1/operations/events")
    event_types = {event["event_type"] for event in events.json()}
    assert {"work_order_created", "sms_queued", "transfer_requested"} <= event_types
