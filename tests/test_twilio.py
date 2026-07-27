import base64

import httpx

from hoa_voice_agent.adapters.twilio import TwilioMessageError, TwilioMessagingAdapter


async def test_twilio_sms_uses_basic_auth_and_form_encoded_data() -> None:
    captured: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured
        captured = request
        return httpx.Response(201, json={"sid": "SM123"})

    adapter = TwilioMessagingAdapter(
        account_sid="AC123",
        auth_token="secret-token",
        from_phone="+15550102000",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    reference = await adapter.send_sms(
        "+15550101111",
        "Pine Grove: work order was created.",
        idempotency_key="sms:work-order-123",
    )

    assert reference == "twilio:SM123"
    assert captured is not None
    assert captured.url.path == "/2010-04-01/Accounts/AC123/Messages.json"
    assert captured.headers["authorization"] == (
        f"Basic {base64.b64encode(b'AC123:secret-token').decode()}"
    )
    assert captured.headers["idempotency-key"] == "sms:work-order-123"
    assert b"To=%2B15550101111" in captured.content
    assert b"From=%2B15550102000" in captured.content


async def test_twilio_error_includes_the_provider_error_code() -> None:
    adapter = TwilioMessagingAdapter(
        account_sid="AC123",
        auth_token="secret-token",
        from_phone="+15550102000",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    400,
                    json={"code": 21408, "message": "Destination region is disabled."},
                )
            )
        ),
    )

    try:
        await adapter.send_sms("+15550101111", "Test")
    except TwilioMessageError as exc:
        assert str(exc) == "Twilio SMS error 21408: Destination region is disabled."
    else:
        raise AssertionError("Expected TwilioMessageError")
