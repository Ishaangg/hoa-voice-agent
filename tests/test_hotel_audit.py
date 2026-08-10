import json
from uuid import uuid4

import pytest

from hoa_voice_agent.services.hotel_audit import HotelConversationAuditLog


@pytest.mark.asyncio
async def test_audit_log_writes_redacted_conversation_events(tmp_path) -> None:
    path = tmp_path / "hotel-conversation-audit.jsonl"
    audit_log = HotelConversationAuditLog(path)
    conversation_id = uuid4()

    await audit_log.record(
        conversation_id,
        "llm_request",
        {
            "payload": {
                "model": "gpt-5.6-luna",
                "api_key": "must-not-be-written",
                "max_completion_tokens": 300,
            },
            "authorization": "Bearer must-not-be-written",
        },
    )

    [line] = path.read_text(encoding="utf-8").splitlines()
    event = json.loads(line)
    assert event["conversation_id"] == str(conversation_id)
    assert event["event"] == "llm_request"
    assert event["data"]["payload"]["model"] == "gpt-5.6-luna"
    assert event["data"]["payload"]["api_key"] == "[REDACTED]"
    assert event["data"]["payload"]["max_completion_tokens"] == 300
    assert event["data"]["authorization"] == "[REDACTED]"
