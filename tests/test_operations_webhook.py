from uuid import uuid4

import httpx

from hoa_voice_agent.adapters.operations_webhook import (
    OperationsWebhookClient,
    WebhookCallControlAdapter,
    WebhookMessagingAdapter,
)


async def test_webhook_sms_uses_authentication_and_work_order_idempotency() -> None:
    captured: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = request
        return httpx.Response(200, json={"reference_id": "sms_123"})

    client = OperationsWebhookClient(
        base_url="https://operations.example",
        bearer_token="test-token",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        reference = await WebhookMessagingAdapter(client).send_sms(
            "+15550101111",
            "Your work order was created.",
            idempotency_key="sms:work-order-123",
        )
    finally:
        await client.close()

    assert reference == "sms_123"
    assert captured is not None
    assert captured.url.path == "/v1/messages/sms"
    assert captured.headers["authorization"] == "Bearer test-token"
    assert captured.headers["idempotency-key"] == "sms:work-order-123"


async def test_webhook_on_call_transfer_has_a_stable_conversation_key() -> None:
    conversation_id = uuid4()
    captured: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = request
        return httpx.Response(200, json={"id": "transfer_123"})

    client = OperationsWebhookClient(
        base_url="https://operations.example",
        bearer_token=None,
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        reference = await WebhookCallControlAdapter(client).transfer_to_on_call(
            conversation_id,
            "+15550102999",
        )
    finally:
        await client.close()

    assert reference == "transfer_123"
    assert captured is not None
    assert captured.headers["idempotency-key"] == f"on-call-transfer:{conversation_id}"
