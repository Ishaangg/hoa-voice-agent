"""Transactional outbox for notifications and other external side effects."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from loguru import logger
from sqlalchemy import and_, or_, select

from hoa_voice_agent.adapters.protocols import MessagingAdapter
from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.hotel_tables import (
    HotelRequestActionRecord,
    HotelRequestRecord,
)
from hoa_voice_agent.persistence.tables import (
    OutboxEventRecord,
    VendorDispatchRecord,
    WorkflowEventRecord,
)


@dataclass(frozen=True)
class OutboxEnvelope:
    id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID | None
    idempotency_key: str
    payload: dict[str, Any]
    attempt_count: int


class OutboxNotificationQueue:
    """Queue notification intents in the caller's current DB transaction."""

    def __init__(self, session) -> None:
        self.session = session

    async def enqueue(
        self,
        *,
        channel: str,
        phone: str,
        message: str,
        idempotency_key: str,
        aggregate_type: str,
        aggregate_id: UUID | None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        existing = await self.session.scalar(
            select(OutboxEventRecord).where(
                OutboxEventRecord.idempotency_key == idempotency_key
            )
        )
        if existing:
            return f"outbox:{existing.id}"

        payload = {
            "channel": channel,
            "phone": phone,
            "message": message,
            **(metadata or {}),
        }
        event = OutboxEventRecord(
            event_type="notification.send",
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            idempotency_key=idempotency_key,
            payload=json.dumps(payload, ensure_ascii=False),
        )
        self.session.add(event)
        await self.session.flush()
        return f"outbox:{event.id}"


class OutboxWorker:
    """Poll and deliver committed notification intents in a separate process."""

    def __init__(
        self,
        database: Database,
        messaging: MessagingAdapter,
        *,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 20,
        max_attempts: int = 8,
        processing_timeout_seconds: int = 300,
    ) -> None:
        self.database = database
        self.messaging = messaging
        self.poll_interval_seconds = max(0.1, poll_interval_seconds)
        self.batch_size = max(1, batch_size)
        self.max_attempts = max(1, max_attempts)
        self.processing_timeout_seconds = max(30, processing_timeout_seconds)
        self._stop = asyncio.Event()

    async def run(self) -> None:
        logger.info(
            "Outbox worker started (batch={}, interval={}s)",
            self.batch_size,
            self.poll_interval_seconds,
        )
        while not self._stop.is_set():
            processed = await self.run_once()
            if not processed:
                try:
                    await asyncio.wait_for(self._stop.wait(), self.poll_interval_seconds)
                except TimeoutError:
                    pass

    async def stop(self) -> None:
        self._stop.set()

    async def run_once(self) -> int:
        events = await self._claim_batch()
        for event in events:
            await self._deliver(event)
        return len(events)

    async def _claim_batch(self) -> list[OutboxEnvelope]:
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=self.processing_timeout_seconds)
        async with self.database.session_factory() as session:
            query = (
                select(OutboxEventRecord)
                .where(
                    or_(
                        and_(
                            OutboxEventRecord.status.in_(("pending", "retry")),
                            OutboxEventRecord.attempt_count < self.max_attempts,
                        ),
                        and_(
                            OutboxEventRecord.status == "processing",
                            OutboxEventRecord.locked_at <= stale_before,
                            OutboxEventRecord.attempt_count <= self.max_attempts,
                        ),
                    ),
                    OutboxEventRecord.available_at <= now,
                    OutboxEventRecord.attempt_count < self.max_attempts,
                )
                .order_by(OutboxEventRecord.created_at)
                .limit(self.batch_size)
            )
            # AsyncSession.bind is not guaranteed to be exposed by every
            # SQLAlchemy async session implementation. The engine is the
            # authoritative dialect source and keeps this worker compatible
            # with both asyncpg and aiosqlite.
            if self.database.engine.url.get_backend_name() == "postgresql":
                query = query.with_for_update(skip_locked=True)
            rows = list(await session.scalars(query))
            result: list[OutboxEnvelope] = []
            for row in rows:
                row.status = "processing"
                row.locked_at = now
                row.attempt_count += 1
                result.append(
                    OutboxEnvelope(
                        id=row.id,
                        event_type=row.event_type,
                        aggregate_type=row.aggregate_type,
                        aggregate_id=row.aggregate_id,
                        idempotency_key=row.idempotency_key,
                        payload=json.loads(row.payload),
                        attempt_count=row.attempt_count,
                    )
                )
            if result:
                await session.commit()
            return result

    async def _deliver(self, event: OutboxEnvelope) -> None:
        payload = event.payload
        channel = str(payload.get("channel", "sms"))
        phone = str(payload.get("phone", ""))
        message = str(payload.get("message", ""))
        try:
            if channel == "whatsapp":
                try:
                    reference = await self.messaging.send_whatsapp(
                        phone, message, idempotency_key=event.idempotency_key
                    )
                except Exception:
                    # Keep the existing vendor behavior: a WhatsApp provider
                    # outage should not lose a dispatch when SMS is available.
                    reference = await self.messaging.send_sms(
                        phone,
                        message,
                        idempotency_key=f"{event.idempotency_key}-sms-fallback",
                    )
            else:
                reference = await self.messaging.send_sms(
                    phone, message, idempotency_key=event.idempotency_key
                )
            await self._complete(event, reference)
        except Exception as exc:
            logger.exception("Outbox event {} delivery failed", event.id)
            await self._fail(event, str(exc))

    async def _complete(self, event: OutboxEnvelope, provider_reference: str) -> None:
        async with self.database.session_factory() as session:
            row = await session.get(OutboxEventRecord, event.id)
            if not row:
                return
            row.status = "sent"
            row.provider_reference = provider_reference
            row.sent_at = datetime.now(UTC)
            row.last_error = None
            await self._update_aggregate(
                session, event, status="sent", detail=provider_reference
            )
            await session.commit()

    async def _fail(self, event: OutboxEnvelope, detail: str) -> None:
        async with self.database.session_factory() as session:
            row = await session.get(OutboxEventRecord, event.id)
            if not row:
                return
            terminal = event.attempt_count >= self.max_attempts
            row.status = "failed" if terminal else "retry"
            row.last_error = detail[:4000]
            row.available_at = datetime.now(UTC) + timedelta(
                seconds=min(300, 2 ** min(event.attempt_count, 8))
            )
            await self._update_aggregate(
                session,
                event,
                status="failed" if terminal else "retry",
                detail=detail,
            )
            await session.commit()

    async def _update_aggregate(
        self, session, event: OutboxEnvelope, *, status: str, detail: str
    ) -> None:
        if event.aggregate_type == "hotel_request" and event.aggregate_id:
            request = await session.get(HotelRequestRecord, event.aggregate_id)
            if request:
                request.notification_status = status
                session.add(
                    HotelRequestActionRecord(
                        request_id=request.id,
                        actor="outbox_worker",
                        action_type=f"notification_{status}",
                        detail=detail,
                    )
                )
        elif event.aggregate_type == "vendor_dispatch" and event.aggregate_id:
            dispatch = await session.get(VendorDispatchRecord, event.aggregate_id)
            if dispatch:
                dispatch.status = status
                dispatch.provider_reference = (
                    detail if status == "sent" else dispatch.provider_reference
                )
                dispatch.failure_detail = None if status == "sent" else detail[:4000]
                if status == "sent":
                    dispatch.sent_at = datetime.now(UTC)
        elif event.aggregate_type == "resident_confirmation" and event.aggregate_id:
            session.add(
                WorkflowEventRecord(
                    conversation_id=event.aggregate_id,
                    event_type=f"confirmation_notification_{status}",
                    detail=detail,
                    reference_id=None,
                )
            )
