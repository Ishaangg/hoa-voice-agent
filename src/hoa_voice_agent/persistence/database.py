from collections.abc import AsyncIterator

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


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(
            self.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        )

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
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
        }
        inspector = inspect(connection)
        for table_name, columns_to_add in additions.items():
            existing = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, column_type in columns_to_add.items():
                if column_name not in existing:
                    connection.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
                    )

    async def sessions(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
