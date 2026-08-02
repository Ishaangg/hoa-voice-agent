from pathlib import Path

import httpx
import pytest

from hoa_voice_agent.api import create_app
from hoa_voice_agent.config import Settings


@pytest.fixture
async def client(tmp_path: Path):
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'console.db'}",
        nvidia_api_key=None,
        sms_mode="fake",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as test_client:
            yield test_client


async def login(client: httpx.AsyncClient, email: str = "ops@evergreen.demo") -> None:
    response = await client.post(
        "/api/v1/console/auth/login",
        json={"email": email, "password": "demo-pass"},
    )
    assert response.status_code == 200


async def create_emergency_case(client: httpx.AsyncClient) -> str:
    start = await client.post(
        "/api/v1/conversations",
        json={"caller_phone": "+15550109999"},
    )
    conversation_id = start.json()["conversation_id"]
    await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "My name is Taylor and I am in Unit 112."},
    )
    await client.post(
        f"/api/v1/conversations/{conversation_id}/turns",
        json={"text": "Water is leaking from the ceiling in the lobby."},
    )
    cases = await client.get("/api/v1/console/cases")
    assert cases.status_code == 401
    return conversation_id


async def test_staff_login_dashboard_and_case_lifecycle(client: httpx.AsyncClient) -> None:
    await create_emergency_case(client)
    await login(client)

    me = await client.get("/api/v1/console/me")
    assert me.json()["role"] == "operations_admin"
    communities = await client.get("/api/v1/console/communities")
    assert [item["name"] for item in communities.json()] == ["Pine Grove Community"]
    dashboard = await client.get("/api/v1/console/dashboard")
    assert dashboard.json()["kpis"]["active_emergencies"] == 1

    cases = await client.get("/api/v1/console/cases")
    case_id = cases.json()[0]["id"]
    detail = await client.get(f"/api/v1/console/cases/{case_id}")
    assert detail.json()["caller"]["unit"] == "Unit 112"
    assert detail.json()["urgency_rationale"]

    calls = await client.get("/api/v1/console/calls")
    assert calls.status_code == 200
    assert calls.json()[0]["work_order_id"] == case_id
    call_detail = await client.get(f"/api/v1/console/calls/{calls.json()[0]['id']}")
    assert call_detail.json()["caller"]["name"] == "Taylor"
    assert len(call_detail.json()["transcript"]) >= 2

    acknowledged = await client.post(f"/api/v1/console/cases/{case_id}/acknowledge")
    assert acknowledged.json()["status"] == "acknowledged"
    assigned = await client.post(
        f"/api/v1/console/cases/{case_id}/assign",
        json={"assignee_type": "on_call", "assigned_to": "Avery Morgan"},
    )
    assert assigned.json()["status"] == "assigned"
    resolved = await client.post(
        f"/api/v1/console/cases/{case_id}/resolve",
        json={"resolution_note": "On-call responder confirmed the leak is isolated."},
    )
    assert resolved.json()["status"] == "resolved"
    detail = await client.get(f"/api/v1/console/cases/{case_id}")
    assert [entry["type"] for entry in detail.json()["timeline"]] == [
        "acknowledged",
        "assigned",
        "resolved",
    ]


async def test_manager_scope_and_approved_knowledge(client: httpx.AsyncClient) -> None:
    await login(client, "manager@evergreen.demo")
    communities = await client.get("/api/v1/console/communities")
    assert [community["name"] for community in communities.json()] == ["Pine Grove Community"]
    community_id = communities.json()[0]["id"]
    created = await client.post(
        f"/api/v1/console/communities/{community_id}/knowledge",
        json={
            "category": "amenity",
            "topic": "clubhouse hours",
            "answer": "The clubhouse is open from 8 AM to 10 PM.",
            "source_reference": "Approved welcome packet, page 3",
            "approval_status": "approved",
        },
    )
    assert created.status_code == 200
    knowledge = await client.get(f"/api/v1/console/communities/{community_id}/knowledge")
    record = next(item for item in knowledge.json() if item["topic"] == "clubhouse hours")
    assert record["approval_status"] == "approved"
    assert record["source_reference"] == "Approved welcome packet, page 3"
    contact = await client.post(
        f"/api/v1/console/communities/{community_id}/routing/contacts",
        json={"contact_type": "on_call", "name": "Jordan Patel", "phone": "+15550101234"},
    )
    assert contact.status_code == 200
    vendor = await client.post(
        f"/api/v1/console/communities/{community_id}/routing/vendors",
        json={
            "incident_type": "plumbing",
            "vendor_name": "ClearFlow Plumbing",
            "contact_phone": "+15550105678",
        },
    )
    assert vendor.status_code == 200
    routing = await client.get(f"/api/v1/console/communities/{community_id}/routing")
    assert any(item["name"] == "Jordan Patel" for item in routing.json()["contacts"])
    assert any(
        item["vendor_name"] == "ClearFlow Plumbing"
        for item in routing.json()["vendor_routes"]
    )
    revisions = await client.get(
        f"/api/v1/console/communities/{community_id}/knowledge/{record['id']}/revisions"
    )
    assert revisions.json()[0]["action"] == "created"
