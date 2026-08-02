from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx

from hoa_voice_agent.adapters.protocols import (
    CallControlAdapter,
    CommunityManagementAdapter,
    MessagingAdapter,
)
from hoa_voice_agent.domain.models import Community, Resident, WorkOrder


class OperationsWebhookClient:
    """Authenticated bridge to the customer's property, messaging, and call systems."""

    def __init__(
        self,
        *,
        base_url: str,
        bearer_token: str | None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._bearer_token = bearer_token
        self._client = (client_factory or (lambda: httpx.AsyncClient(timeout=10.0)))()

    async def close(self) -> None:
        await self._client.aclose()

    async def post(self, path: str, payload: dict[str, Any], *, idempotency_key: str) -> str:
        headers = {"Idempotency-Key": idempotency_key}
        if self._bearer_token:
            headers["Authorization"] = f"Bearer {self._bearer_token}"
        response = await self._client.post(
            f"{self._base_url}{path}",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        body = response.json()
        reference = body.get("reference_id") or body.get("id")
        if not isinstance(reference, str) or not reference:
            raise ValueError(f"Operations webhook {path} returned no reference_id")
        return reference


class WebhookCommunityManagementAdapter(CommunityManagementAdapter):
    """Uses the configured directory for reads and a real webhook for work-order writes."""

    def __init__(
        self,
        directory: CommunityManagementAdapter,
        operations: OperationsWebhookClient,
    ) -> None:
        self._directory = directory
        self._operations = operations

    async def get_community(self, community_id: UUID) -> Community | None:
        return await self._directory.get_community(community_id)

    async def find_resident(self, phone: str) -> Resident | None:
        return await self._directory.find_resident(phone)

    async def create_work_order(self, work_order: WorkOrder) -> WorkOrder:
        await self._operations.post(
            "/v1/work-orders",
            {
                "work_order_id": str(work_order.id),
                "conversation_id": str(work_order.conversation_id),
                "community_id": str(work_order.community_id),
                "resident_id": str(work_order.resident_id) if work_order.resident_id else None,
                "incident_type": work_order.incident_type.value,
                "priority": work_order.priority.value,
                "description": work_order.description,
                "status": work_order.status,
                "assigned_to": work_order.assigned_to,
                "assignee_type": work_order.assignee_type,
                "price_status": work_order.price_status,
                "price_shown_to_caller": work_order.price_shown_to_caller,
                "resident_consent": work_order.resident_consent,
                "quote_status": work_order.quote_status,
                "quote_amount_cents": work_order.quote_amount_cents,
                "final_amount_cents": work_order.final_amount_cents,
                "billed_to": work_order.billed_to,
            },
            idempotency_key=f"work-order:{work_order.id}",
        )
        return work_order


class WebhookMessagingAdapter(MessagingAdapter):
    def __init__(self, operations: OperationsWebhookClient) -> None:
        self._operations = operations

    async def send_sms(
        self,
        phone: str,
        message: str,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        return await self._operations.post(
            "/v1/messages/sms",
            {"to": phone, "message": message},
            idempotency_key=idempotency_key or f"sms:{phone}:{message}",
        )

    async def send_whatsapp(
        self,
        phone: str,
        message: str,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        return await self._operations.post(
            "/v1/messages/whatsapp",
            {"to": phone, "message": message},
            idempotency_key=idempotency_key or f"whatsapp:{phone}:{message}",
        )


class WebhookCallControlAdapter(CallControlAdapter):
    def __init__(self, operations: OperationsWebhookClient) -> None:
        self._operations = operations

    async def transfer_to_on_call(self, conversation_id: UUID, phone: str) -> str:
        return await self._operations.post(
            "/v1/on-call-transfers",
            {"conversation_id": str(conversation_id), "on_call_phone": phone},
            idempotency_key=f"on-call-transfer:{conversation_id}",
        )
