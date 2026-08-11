import json

import httpx

from hoa_voice_agent.services.hotel_turn_interpreter import OpenAIHotelTurnInterpreter


async def test_interpreter_returns_structured_json() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"maintenance","action":"create_request",'
                                '"service_category":"maintenance",'
                                '"service_description":"Bathroom has no hot water",'
                                '"room_number":204,"confidence":0.94}'
                            )
                        }
                    }
                ]
            },
        )
    )
    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    result = await interpreter.interpret(
        text="The shower has gone cold in 204.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category=None,
        pending_description=None,
        active_topic=None,
        last_intent=None,
    )

    assert result is not None
    assert result.intent == "maintenance"
    assert result.action == "create_request"
    assert result.room_number == "204"


async def test_groq_llama_request_omits_unsupported_reasoning_effort() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if "reasoning_effort" in payload:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "`reasoning_effort` is not supported with this model"
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"booking","action":"ask_slot",'
                                '"booking_guest_count":3,'
                                '"booking_guest_count_evidence":"teen logon",'
                                '"confidence":0.99}'
                            )
                        }
                    }
                ]
            },
        )

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://api.groq.com/openai/v1",
        model="llama-3.3-70b-versatile",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    result = await interpreter.interpret(
        text="Mujhe teen logon ke liye room chahiye.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category="booking",
        pending_description=None,
        active_topic="booking",
        last_intent="booking",
    )

    assert result is not None
    assert result.booking_guest_count == 3
    assert len(requests) == 1
    assert "reasoning_effort" not in requests[0]
    assert requests[0]["response_format"] == {"type": "json_object"}


async def test_compact_context_keeps_pending_booking_fact_confirmation() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"confirmation","action":"confirm",'
                                '"dialogue_act":"accept","command":"accept_offer"}'
                            )
                        }
                    }
                ]
            },
        )

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://api.groq.com/openai/v1",
        model="qwen/qwen3.6-27b",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    result = await interpreter.interpret(
        text="\u0939\u093e\u0902 \u091c\u0940.",
        last_assistant_turn=(
            "Bas confirm kar loon, kya main guest count ko 3 guests maan loon?"
        ),
        caller_name=None,
        room_number=None,
        pending_category="booking",
        pending_description="guest count confirmation",
        active_topic="booking",
        last_intent="booking",
        booking_guest_count=2,
        booking_plan_json="{}",
        booking_pending_fact_confirmation=True,
    )

    assert result is not None
    request_context = json.loads(requests[0]["messages"][1]["content"])
    assert request_context["booking_pending_fact_confirmation"] is True


async def test_uncertain_party_answer_gets_one_semantic_repair_attempt() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            content = json.dumps(
                {
                    "intent": "unknown",
                    "action": "none",
                    "dialogue_act": "unknown",
                    "command": "unknown",
                }
            )
        else:
            content = json.dumps(
                {
                    "intent": "booking",
                    "action": "answer",
                    "dialogue_act": "inform",
                    "command": "update_facts",
                    "booking_plan_json": {
                        "party_composition_known": True,
                        "party_groups": [],
                    },
                }
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}]},
        )

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://api.groq.com/openai/v1",
        model="qwen/qwen3.6-27b",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    result = await interpreter.interpret(
        text=(
            "\u0928\u0939\u0940\u0902 \u0915\u094b\u0908 \u092d\u0940 couple "
            "\u0928\u0939\u0940\u0902 \u0939\u0948."
        ),
        last_assistant_turn=(
            "Theek hai, 3 guests hain. Kya group mein couples hain? Agar haan, kitne?"
        ),
        caller_name=None,
        room_number=None,
        pending_category="booking",
        pending_description=None,
        active_topic="booking",
        last_intent="booking",
        response_language="hinglish",
        booking_guest_count=3,
        booking_plan_json="{}",
    )

    assert result is not None
    assert result.intent == "booking"
    assert result.dialogue_act == "inform"
    assert result.booking_plan_json is not None
    assert json.loads(result.booking_plan_json) == {
        "party_composition_known": True,
        "party_groups": [],
    }
    assert len(requests) == 2


async def test_interpreter_preserves_guest_count_evidence_from_model() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"booking","action":"ask_slot",'
                                '"booking_guest_count":4,'
                                '"booking_guest_count_evidence":"चार लोगों के लिए",'
                                '"confidence":0.94}'
                            )
                        }
                    }
                ]
            },
        )
    )
    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    result = await interpreter.interpret(
        text="Mujhe चार लोगों के लिए room chahiye.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category="booking",
        pending_description=None,
        active_topic="booking",
        last_intent="booking",
    )

    assert result is not None
    assert result.booking_guest_count == 4
    assert result.booking_guest_count_evidence == "चार लोगों के लिए"


async def test_interpreter_treats_empty_optional_booking_numbers_as_missing() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"booking","action":"ask_slot",'
                                '"booking_guest_count":"",'
                                '"booking_nights":"",'
                                '"booking_guest_count_evidence":"",'
                                '"confidence":0.75}'
                            )
                        }
                    }
                ]
            },
        )
    )
    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    result = await interpreter.interpret(
        text="Mujhe room chahiye.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category="booking",
        pending_description=None,
        active_topic="booking",
        last_intent="booking",
    )

    assert result is not None
    assert result.booking_guest_count is None
    assert result.booking_nights is None


async def test_interpreter_keeps_guest_count_when_optional_boolean_is_empty() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"booking","action":"ask_slot",'
                                '"booking_guest_count":2,'
                                '"booking_accessibility_required":"",'
                                '"confidence":0.9}'
                            )
                        }
                    }
                ]
            },
        )
    )
    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    result = await interpreter.interpret(
        text="Mujhe do logon ke liye hotel book karwana hai.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category="booking",
        pending_description=None,
        active_topic="booking",
        last_intent="booking",
    )

    assert result is not None
    assert result.booking_guest_count == 2
    assert result.booking_accessibility_required is None
    assert result.booking_guest_count_evidence is None


async def test_interpreter_preserves_booking_facts_when_optional_metadata_is_invalid() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"booking","action":"ask_slot",'
                                '"speech_status":"understood",'
                                '"service_category":"",'
                                '"booking_guest_count":2,'
                                '"booking_guest_count_evidence":"do logon ke liye",'
                                '"confidence":0.9}'
                            )
                        }
                    }
                ]
            },
        )
    )
    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    result = await interpreter.interpret(
        text="Mujhe booking karvani hai do logon ke liye.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category="booking",
        pending_description=None,
        active_topic="booking",
        last_intent="booking",
    )

    assert result is not None
    assert result.intent == "booking"
    assert result.booking_guest_count == 2
    assert result.booking_guest_count_evidence == "do logon ke liye"
    assert result.service_category is None
    assert result.speech_status == "complete"


async def test_interpreter_receives_hinglish_response_language() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"general","action":"answer",'
                                '"response":"Bilkul, main help karti hoon.",'
                                '"confidence":0.99}'
                            )
                        }
                    }
                ]
            },
        )

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    result = await interpreter.interpret(
        text="Mujhe room book karna hai.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category=None,
        pending_description=None,
        active_topic=None,
        last_intent=None,
        response_language="hinglish",
    )

    assert result is not None
    payload = json.loads(requests[0].content)
    context = json.loads(payload["messages"][1]["content"])
    assert context["response_language"] == "hinglish"
    assert context["guest_text"] == "Mujhe room book karna hai."
    assert "Roman-script Hindi" in payload["messages"][0]["content"]
    assert "guest_text is verbatim STT" in payload["messages"][0]["content"]
    response_format = payload["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    schema = response_format["json_schema"]["schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


async def test_interpreter_falls_back_only_for_legacy_provider_schema_rejection() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "response_format json_schema is not supported"
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"general","action":"answer",'
                                '"response":"Bilkul.","confidence":0.99}'
                            )
                        }
                    }
                ]
            },
        )

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    result = await interpreter.interpret(
        text="Bas itna hi poochna tha.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category=None,
        pending_description=None,
        active_topic=None,
        last_intent=None,
    )

    assert result is not None
    assert result.response == "Bilkul."
    assert len(requests) == 2
    first_payload = json.loads(requests[0].content)
    second_payload = json.loads(requests[1].content)
    assert first_payload["response_format"]["type"] == "json_schema"
    assert second_payload["response_format"] == {"type": "json_object"}


async def test_interpreter_normalizes_an_object_booking_plan_to_json() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"booking","action":"answer",'
                                '"booking_plan_json":{"standard":{"quantity":1}},'
                                '"confidence":0.94}'
                            )
                        }
                    }
                ]
            },
        )
    )
    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=transport),
    )

    result = await interpreter.interpret(
        text="I need one standard room.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category=None,
        pending_description=None,
        active_topic=None,
        last_intent=None,
    )

    assert result is not None
    assert result.booking_plan_json == '{"standard": {"quantity": 1}}'


async def test_interpreter_uses_a_read_only_tool_without_a_second_llm_call() -> None:
    calls = 0
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        requests.append(request)
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "search_rooms",
                                                "arguments": (
                                                    '{"check_in":"2030-05-01",'
                                                    '"check_out":"2030-05-03",'
                                                    '"guest_count":2,'
                                                    '"guest_count_evidence":"two guests"}'
                                                ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        raise AssertionError("the tool path must not make a second LLM request")

    progress: list[str] = []
    tool_arguments: list[dict] = []

    async def execute(name: str, arguments: dict) -> dict:
        assert name == "search_rooms"
        tool_arguments.append(arguments)
        return {"rooms": [{"room_type": "standard", "max_occupancy": 2}]}

    async def report_progress(phrase: str) -> None:
        progress.append(phrase)

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await interpreter.interpret(
        text="Can I book a room for two guests?",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category=None,
        pending_description=None,
        active_topic=None,
        last_intent=None,
        on_response=report_progress,
        tool_executor=execute,
    )

    assert result is not None
    assert result.response is None
    assert tool_arguments == [
        {
            "check_in": "2030-05-01",
            "check_out": "2030-05-03",
            "guest_count": 2,
            "guest_count_evidence": "two guests",
        }
    ]
    assert calls == 1
    assert progress == ["Let me check the available room options for you."]
    first_payload = json.loads(requests[0].content)
    assert first_payload["reasoning_effort"] == "none"
    assert first_payload["max_completion_tokens"] == 300
    assert "max_tokens" not in first_payload
    assert len(requests) == 1
    assert result.read_only_tools == ("search_rooms",)
    assert result.booking_guest_count == 2
    assert result.tool_results["search_rooms"]["rooms"][0]["room_type"] == "standard"


async def test_interpreter_does_not_retry_or_summarize_after_a_tool_result() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "search_rooms",
                                            "arguments": (
                                                '{"check_in":"2030-05-01",'
                                                '"check_out":"2030-05-03",'
                                                '"guest_count":2,'
                                                '"guest_count_evidence":"two guests"}'
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        raise AssertionError("the tool path must not make a second LLM request")

    async def execute(_name: str, _arguments: dict) -> dict:
        return {"rooms": [{"room_type": "standard", "max_occupancy": 2}]}

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    result = await interpreter.interpret(
        text="Can I book a room for two guests?",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category=None,
        pending_description=None,
        active_topic=None,
        last_intent=None,
        tool_executor=execute,
    )

    assert result is not None
    assert result.response is None
    assert calls == 1


async def test_interpreter_rejects_guest_count_not_supported_by_caller() -> None:
    executed: list[dict] = []
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"intent":"booking","action":"answer",'
                                    '"response":"I need the number of guests first."}'
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_rooms",
                                        "arguments": (
                                            '{"check_in":"2030-05-01",'
                                            '"check_out":"2030-05-03",'
                                            '"guest_count":1}'
                                        ),
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    async def execute(_name: str, arguments: dict) -> dict:
        executed.append(arguments)
        return {"rooms": []}

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    result = await interpreter.interpret(
        text="Mujhe 1 August se 3 August tak room chahiye.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category=None,
        pending_description=None,
        active_topic=None,
        last_intent=None,
        response_language="hinglish",
        tool_executor=execute,
    )

    assert result is not None
    assert result.tool_error is None
    assert result.action == "ask_slot"
    assert result.booking_check_in == "2030-05-01"
    assert result.booking_check_out == "2030-05-03"
    assert result.response is not None
    assert "guests" in result.response
    assert executed == []
    assert calls == 1


async def test_interpreter_accepts_guest_count_from_model_evidence() -> None:
    executed: list[dict] = []
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "search_rooms",
                                            "arguments": json.dumps(
                                                {
                                                    "check_in": "2030-05-01",
                                                    "check_out": "2030-05-03",
                                                    "guest_count": 4,
                                                    "guest_count_evidence": "चार लोगों के लिए",
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        raise AssertionError("the tool path must not make a second LLM request")

    async def execute(_name: str, arguments: dict) -> dict:
        executed.append(arguments)
        return {"rooms": [{"room_type": "standard"}]}

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ),
    )

    result = await interpreter.interpret(
        text="Mujhe चार लोगों के लिए room chahiye.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category="booking",
        pending_description=None,
        active_topic="booking",
        last_intent="booking",
        tool_executor=execute,
    )

    assert result is not None
    assert result.response is None
    assert result.booking_guest_count == 4
    assert executed == [
        {
            "check_in": "2030-05-01",
            "check_out": "2030-05-03",
            "guest_count": 4,
            "guest_count_evidence": "चार लोगों के लिए",
        }
    ]


async def test_interpreter_audits_payload_and_validated_tool_arguments() -> None:
    calls = 0
    audit_events: list[tuple[str, dict]] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "search_rooms",
                                            "arguments": (
                                                '{"check_in":"2030-05-01",'
                                                '"check_out":"2030-05-03",'
                                                '"guest_count":2,'
                                                '"guest_count_evidence":"two guests"}'
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"intent":"booking","action":"answer",'
                                '"response":"Two rooms are available."}'
                            )
                        }
                    }
                ]
            },
        )

    async def execute(_name: str, _arguments: dict) -> dict:
        return {"rooms": []}

    async def record_audit(event: str, data: dict) -> None:
        audit_events.append((event, data))

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await interpreter.interpret(
        text="Find a room for two guests.",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category=None,
        pending_description=None,
        active_topic=None,
        last_intent=None,
        audit_event=record_audit,
        tool_executor=execute,
    )

    assert result is not None
    assert [event for event, _ in audit_events] == [
        "llm_request",
        "validated_tool_calls",
    ]
    assert audit_events[0][1]["phase"] == "initial"
    assert audit_events[0][1]["payload"]["model"] == "test-model"
    assert audit_events[1][1]["tools"] == [
        {
            "name": "search_rooms",
            "arguments": {
                "check_in": "2030-05-01",
                "check_out": "2030-05-03",
                "guest_count": 2,
                "guest_count_evidence": "two guests",
            },
        }
    ]


async def test_interpreter_rotates_progress_phrases_for_consecutive_room_searches() -> None:
    tool_response = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "search_rooms",
                                "arguments": (
                                                '{"check_in":"2030-05-01",'
                                                '"check_out":"2030-05-03",'
                                                '"guest_count":2,'
                                                '"guest_count_evidence":"two guests"}'
                                            ),
                            },
                        }
                    ],
                }
            }
        ]
    }
    responses = [tool_response] * 3
    progress: list[str] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=responses.pop(0))

    async def execute(_name: str, _arguments: dict) -> dict:
        return {"rooms": []}

    async def report_progress(phrase: str) -> None:
        progress.append(phrase)

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    for _ in range(3):
        result = await interpreter.interpret(
            text="What rooms are available for two guests?",
            last_assistant_turn=None,
            caller_name=None,
            room_number=None,
            pending_category=None,
            pending_description=None,
            active_topic=None,
            last_intent=None,
            on_response=report_progress,
            tool_executor=execute,
        )
        assert result is not None

    assert progress == [
        "Let me check the available room options for you.",
        "I'll look up availability for those dates.",
        "Let me see what rooms are open for your stay.",
    ]


async def test_interpreter_stops_after_invalid_tool_arguments() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_rooms",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    async def execute(_name: str, _arguments: dict) -> dict:
        raise AssertionError("Invalid tool arguments must not reach the executor")

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await interpreter.interpret(
        text="What rooms do you have?",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category=None,
        pending_description=None,
        active_topic=None,
        last_intent=None,
        tool_executor=execute,
    )

    assert result is not None
    assert result.tool_error is None
    assert result.action == "ask_slot"
    assert result.response is not None
    assert "guests" in result.response
    assert calls == 1


def test_empty_search_guest_count_uses_persisted_booking_context() -> None:
    completed = OpenAIHotelTurnInterpreter._complete_tool_arguments(
        "search_rooms",
        {"check_in": "2030-05-01", "check_out": "2030-05-03", "guest_count": ""},
        {"booking_guest_count": 2, "guest_text": ""},
    )

    assert completed["guest_count"] == 2


async def test_interpreter_completes_an_omitted_search_slot_from_booking_context() -> None:
    calls = 0
    executed_arguments: list[dict] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "search_rooms",
                                            "arguments": '{"room_type":"standard"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"intent":"booking","action":"answer",'
                            '"response":"Standard rooms are available."}'
                        }
                    }
                ]
            },
        )

    async def execute(name: str, arguments: dict) -> dict:
        assert name == "search_rooms"
        executed_arguments.append(arguments)
        return {"rooms": []}

    interpreter = OpenAIHotelTurnInterpreter(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="test-model",
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await interpreter.interpret(
        text="Standard",
        last_assistant_turn=None,
        caller_name=None,
        room_number=None,
        pending_category="booking",
        pending_description=None,
        active_topic="booking",
        last_intent="booking",
        booking_check_in="2026-09-20",
        booking_check_out="2026-09-22",
        booking_guest_count=2,
        tool_executor=execute,
    )

    assert result is not None
    assert result.tool_error is None
    assert executed_arguments == [
        {
            "check_in": "2026-09-20",
            "check_out": "2026-09-22",
            "guest_count": 2,
            "room_type": "standard",
        }
    ]
