from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class ResilientAsyncSession(AsyncSession):
    """Rollback failed transactions before returning the session to callers.

    Voice sessions keep one database session alive for the duration of a call.
    A transient SQLite write failure (usually another writer briefly holding the
    file lock) otherwise leaves SQLAlchemy in ``PendingRollbackError`` state and
    causes every later turn in that call to fail as well.
    """

    async def commit(self) -> None:
        try:
            await super().commit()
        except Exception:
            await self._rollback_after_failure()
            raise

    async def execute(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await super().execute(*args, **kwargs)
        except Exception:
            await self._rollback_after_failure()
            raise

    async def scalar(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await super().scalar(*args, **kwargs)
        except Exception:
            await self._rollback_after_failure()
            raise

    async def get(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await super().get(*args, **kwargs)
        except Exception:
            await self._rollback_after_failure()
            raise

    async def _rollback_after_failure(self) -> None:
        try:
            await super().rollback()
        except Exception:
            # Preserve the original database error. The session will be
            # closed by its owning request/call scope.
            pass


class Database:
    def __init__(self, url: str, *, auto_create_schema: bool = True) -> None:
        self.auto_create_schema = auto_create_schema
        engine_options = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            # The API console and voice sessions can write the same local SQLite
            # file at the same time. Wait for a short-lived writer lock instead
            # of failing after the sqlite default timeout.
            engine_options["connect_args"] = {"timeout": 30}
        self.engine: AsyncEngine = create_async_engine(url, **engine_options)
        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            class_=ResilientAsyncSession,
        )

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            if self.engine.url.get_backend_name() == "sqlite":
                # WAL lets the operations console read while a voice session
                # records a turn. The busy timeout covers the remaining short
                # writer/writer windows.
                await connection.execute(text("PRAGMA busy_timeout=30000"))
                await connection.execute(text("PRAGMA journal_mode=WAL"))
            if not self.auto_create_schema:
                migrated = await connection.run_sync(
                    lambda sync_connection: inspect(sync_connection).has_table("alembic_version")
                )
                if not migrated:
                    raise RuntimeError(
                        "Database schema is migration-managed. Run `alembic upgrade head` "
                        "before starting the application."
                    )
                return
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(self._apply_additive_migrations)

    @staticmethod
    def _apply_additive_migrations(connection) -> None:
        """Keep the local prototype database compatible with additive schema changes."""

        additions = {
            "conversations": {
                "caller_name": "VARCHAR(120)",
                "caller_unit": "VARCHAR(120)",
                "pending_caller_name": "VARCHAR(120)",
                "pending_caller_unit": "VARCHAR(120)",
            },
            "work_orders": {
                "assigned_to": "VARCHAR(120)",
                "assignee_type": "VARCHAR(32)",
                "acknowledged_at": "DATETIME",
                "resolved_at": "DATETIME",
                "resolution_note": "TEXT",
                "updated_at": "DATETIME",
                "price_status": "VARCHAR(16) DEFAULT 'unknown'",
                "price_shown_to_caller": "TEXT",
                "resident_consent": "VARCHAR(24) DEFAULT 'not_required'",
                "quote_status": "VARCHAR(24) DEFAULT 'not_required'",
                "quote_amount_cents": "INTEGER",
                "quote_approver": "VARCHAR(120)",
                "final_amount_cents": "INTEGER",
                "billed_to": "VARCHAR(32)",
                "dispatch_route_id": "VARCHAR(36)",
            },
            "vendor_routes": {
                "vendor_id": "VARCHAR(36)",
                "dispatch_mode": "VARCHAR(32) DEFAULT 'auto_up_to_limit'",
                "diagnostic_fee_cents": "INTEGER",
                "automatic_approval_limit_cents": "INTEGER",
                "payer_responsibility": "VARCHAR(32) DEFAULT 'confirm_before_booking'",
            },
            "community_knowledge": {
                "source_reference": "TEXT",
                "approval_status": "VARCHAR(32) DEFAULT 'approved'",
                "approved_by": "VARCHAR(120)",
                "approved_at": "DATETIME",
            },
            "vendor_dispatches": {
                "vendor_phone": "VARCHAR(32)",
                "last_inbound_reference": "VARCHAR(160)",
                "last_action_at": "DATETIME",
            },
            "hotel_conversations": {
                "active_topic": "VARCHAR(64)",
                "last_intent": "VARCHAR(64)",
                "last_turn_confidence": "FLOAT",
                "pending_category": "VARCHAR(64)",
                "pending_description": "TEXT",
                "deferred_category": "VARCHAR(64)",
                "deferred_description": "TEXT",
                "deferred_state": "VARCHAR(64)",
                "deferred_turns": "INTEGER DEFAULT 0",
                "booking_check_in": "DATE",
                "booking_check_out": "DATE",
                "booking_guest_count": "INTEGER",
                "booking_nights": "INTEGER",
                "booking_room_preference": "VARCHAR(64)",
                "booking_last_search_result": "TEXT",
                "booking_plan_json": "TEXT",
                "booking_plan_version": "INTEGER DEFAULT 0",
                "booking_hold_reservation_id": "VARCHAR(36)",
                "booking_offer_id": "VARCHAR(36)",
                "booking_catalog_snapshot_version": "INTEGER",
                "booking_catalog_relevant_json": "TEXT",
                "booking_criteria_json": "TEXT",
                "booking_criteria_revision": "INTEGER DEFAULT 0",
                "booking_facts_json": "TEXT",
                "booking_fact_revision": "INTEGER DEFAULT 0",
                "booking_response_scope": "VARCHAR(32)",
            },
            "hotel_rooms": {
                "max_occupancy": "INTEGER DEFAULT 2",
            },
            "hotel_reservations": {
                "hold_expires_at": "DATETIME",
                "booking_plan_version": "INTEGER DEFAULT 0",
                "nightly_total_cents": "INTEGER DEFAULT 0",
            },
        }
        inspector = inspect(connection)
        for table_name, columns_to_add in additions.items():
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, column_type in columns_to_add.items():
                if column_name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                    )
        # Earlier demo versions stored an in-progress reservation by changing
        # the physical room itself to "held". Reservation records now own
        # dated holds, so normalize those stale room flags while legacy
        # reservation.room_id continues to protect its actual stay dates.
        if inspector.has_table("hotel_rooms"):
            connection.execute(
                text("UPDATE hotel_rooms SET status = 'available' WHERE status = 'held'")
            )

    async def sessions(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
