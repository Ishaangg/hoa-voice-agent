"""Run the notification outbox as a separate production process.

The voice/media worker and the control-plane API must not be responsible for
retrying provider calls. This process owns that side effect boundary and can be
scaled or restarted independently without replaying a live conversation.

Run with::

    python -m hoa_voice_agent.voice.outbox_worker

In production, set ``DATABASE_AUTO_CREATE=false`` and run ``alembic upgrade
head`` before starting this process.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from loguru import logger

from hoa_voice_agent.adapters.fake import FakeMessagingAdapter
from hoa_voice_agent.adapters.operations_webhook import (
    OperationsWebhookClient,
    WebhookMessagingAdapter,
)
from hoa_voice_agent.adapters.twilio import TwilioMessagingAdapter
from hoa_voice_agent.config import get_settings
from hoa_voice_agent.persistence.database import Database
from hoa_voice_agent.services.outbox import OutboxWorker


def _build_messaging(settings, operations: OperationsWebhookClient | None):
    if settings.sms_mode == "twilio":
        if not all(
            (settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_from_phone)
        ):
            raise RuntimeError(
                "TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, and TWILIO_FROM_PHONE are required "
                "for the outbox worker."
            )
        if settings.vendor_notification_channel == "whatsapp" and not settings.twilio_whatsapp_from:
            raise RuntimeError(
                "TWILIO_WHATSAPP_FROM is required when vendor notifications use WhatsApp."
            )
        return TwilioMessagingAdapter(
            account_sid=settings.twilio_account_sid,
            auth_token=settings.twilio_auth_token,
            from_phone=settings.twilio_from_phone,
            whatsapp_from=settings.twilio_whatsapp_from,
            whatsapp_content_sid=settings.twilio_whatsapp_content_sid,
        )
    if settings.sms_mode == "webhook":
        if operations is None:
            raise RuntimeError("SMS_MODE=webhook requires OPERATIONS_MODE=webhook.")
        return WebhookMessagingAdapter(operations)
    return FakeMessagingAdapter()


async def main() -> None:
    settings = get_settings()
    database = Database(
        settings.database_url,
        auto_create_schema=settings.database_auto_create,
    )
    operations: OperationsWebhookClient | None = None
    if settings.operations_mode == "webhook":
        if not settings.operations_webhook_base_url:
            raise RuntimeError(
                "OPERATIONS_WEBHOOK_BASE_URL is required when OPERATIONS_MODE=webhook."
            )
        operations = OperationsWebhookClient(
            base_url=settings.operations_webhook_base_url,
            bearer_token=settings.operations_webhook_bearer_token,
        )
    try:
        await database.create_schema()
        messaging = _build_messaging(settings, operations)
        worker = OutboxWorker(
            database,
            messaging,
            poll_interval_seconds=settings.outbox_poll_interval_seconds,
            batch_size=settings.outbox_batch_size,
            max_attempts=settings.outbox_max_attempts,
            processing_timeout_seconds=settings.outbox_processing_timeout_seconds,
        )
        logger.info("Starting production notification outbox worker")
        await worker.run()
    finally:
        if operations:
            await operations.close()
        await database.dispose()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
