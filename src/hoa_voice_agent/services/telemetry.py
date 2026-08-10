"""Non-blocking, privacy-conscious voice telemetry.

The audio loop should never wait for an analytics INSERT. ``CallTelemetry``
buffers small metric events and writes them through a fresh database session in
the background. Metrics contain timings and stable labels, not transcript text.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from loguru import logger

from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.tables import VoiceMetricRecord


@dataclass(frozen=True)
class TelemetryEvent:
    metric_name: str
    value_ms: int | None
    metadata: dict[str, Any]


class CallTelemetry:
    """Record per-call metrics without coupling them to the live DB session."""

    def __init__(
        self,
        database: Database,
        *,
        call_id: str,
        conversation_id: UUID | None,
        domain: str,
    ) -> None:
        self.database = database
        self.call_id = call_id
        self.conversation_id = conversation_id
        self.domain = domain
        self._events: asyncio.Queue[TelemetryEvent | None] = asyncio.Queue()
        self._writer_task: asyncio.Task | None = None
        self._closed = False

    def record(
        self,
        metric_name: str,
        value_ms: int | float | None = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Queue one event; this method intentionally does not await I/O."""

        if self._closed:
            return
        if self._writer_task is None:
            try:
                self._writer_task = asyncio.create_task(self._writer())
            except RuntimeError:
                logger.debug("Telemetry event dropped outside an active event loop")
                return
        self._events.put_nowait(
            TelemetryEvent(
                metric_name=metric_name,
                value_ms=round(float(value_ms)) if value_ms is not None else None,
                metadata=metadata or {},
            )
        )

    async def _writer(self) -> None:
        while True:
            event = await self._events.get()
            try:
                if event is None:
                    return
                async with self.database.session_factory() as session:
                    session.add(
                        VoiceMetricRecord(
                            call_id=self.call_id,
                            conversation_id=self.conversation_id,
                            domain=self.domain,
                            metric_name=event.metric_name,
                            value_ms=event.value_ms,
                            metadata_json=json.dumps(event.metadata, ensure_ascii=False),
                        )
                    )
                    await session.commit()
            except Exception:
                # Observability must never interrupt the voice call. The
                # event is logged with labels only; no transcript is included.
                logger.exception("Failed to persist voice metric {}", event)
            finally:
                self._events.task_done()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer_task is None:
            return
        await self._events.join()
        self._events.put_nowait(None)
        await self._writer_task
