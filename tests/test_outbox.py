import json
from uuid import uuid4

from sqlalchemy import select

from hoa_voice_agent.adapters.fake import FakeMessagingAdapter
from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.persistence.tables import OutboxEventRecord
from hoa_voice_agent.services.outbox import OutboxNotificationQueue, OutboxWorker


async def test_outbox_deduplicates_and_delivers_once() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    messaging = FakeMessagingAdapter()
    aggregate_id = uuid4()
    async with database.session_factory() as session:
        queue = OutboxNotificationQueue(session)
        first = await queue.enqueue(
            channel="whatsapp",
            phone="+15550101111",
            message="Service request",
            idempotency_key="dispatch:one",
            aggregate_type="vendor_dispatch",
            aggregate_id=aggregate_id,
        )
        second = await queue.enqueue(
            channel="whatsapp",
            phone="+15550101111",
            message="Service request",
            idempotency_key="dispatch:one",
            aggregate_type="vendor_dispatch",
            aggregate_id=aggregate_id,
        )
        await session.commit()

    assert first == second
    worker = OutboxWorker(database, messaging)
    assert await worker.run_once() == 1
    assert messaging.whatsapp_messages == [("+15550101111", "Service request")]

    async with database.session_factory() as session:
        event = await session.scalar(select(OutboxEventRecord))
        assert event is not None
        assert event.status == "sent"
        assert event.provider_reference.startswith("whatsapp-simulated-")
        assert json.loads(event.payload)["channel"] == "whatsapp"
    await database.dispose()
