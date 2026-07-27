import json

import httpx

from hoa_voice_agent.services.nvidia_conversation_responder import NvidiaConversationResponder


async def test_nemotron_conversation_uses_non_thinking_mode() -> None:
    captured_request: httpx.Request | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_request
        captured_request = request
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Hello! I am doing well."}}]},
        )

    responder = NvidiaConversationResponder(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    reply = await responder.reply(
        text="Hello, how are you?",
        community_name="Maple Grove",
        faqs={"pool hours": "The pool is open from 8 AM to 9 PM."},
    )

    assert reply == "Hello! I am doing well."
    assert captured_request is not None
    payload = json.loads(captured_request.content)
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["stream"] is False
    assert payload["max_tokens"] == 80
