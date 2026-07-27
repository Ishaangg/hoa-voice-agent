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
    return response.json()["conversation_id"]


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_known_resident_gets_personalized_greeting(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/v1/conversations",
        json={"caller_phone": "+15550101111"},
    )

    assert response.status_code == 201
    assert response.json()["say"].startswith("Hi Alex")
    assert response.json()["resident_id"] is not None


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
