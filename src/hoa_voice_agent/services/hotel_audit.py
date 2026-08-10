"""Durable, local audit events for hotel LLM and PMS activity."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from loguru import logger

_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "token",
    }
)


class HotelConversationAuditLog:
    """Append sanitized, per-conversation trace events to one JSONL file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = asyncio.Lock()

    async def record(
        self,
        conversation_id: UUID,
        event: str,
        data: Mapping[str, Any],
    ) -> None:
        """Persist one event without allowing audit failures to affect a call."""

        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "conversation_id": str(conversation_id),
            "event": event,
            "data": _redact(data),
        }
        line = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str) + "\n"
        try:
            async with self._lock:
                await asyncio.to_thread(self._append, line)
        except Exception:
            logger.exception("Unable to write hotel conversation audit event={}", event)

    def _append(self, line: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as file:
            file.write(line)


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]"
            if _is_sensitive_field(str(key))
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item) for item in value]
    return value


def _is_sensitive_field(name: str) -> bool:
    normalized = name.casefold().replace("-", "_")
    return normalized in _SENSITIVE_FIELD_NAMES or normalized.endswith(
        ("_api_key", "_password", "_secret", "_token")
    )
