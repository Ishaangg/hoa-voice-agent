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
            await connection.run_sync(self._apply_additive_conversation_migrations)

    @staticmethod
    def _apply_additive_conversation_migrations(connection) -> None:
        """Keep the local prototype database compatible with additive schema changes."""

        columns = {column["name"] for column in inspect(connection).get_columns("conversations")}
        for name in (
            "caller_name",
            "caller_unit",
            "pending_caller_name",
            "pending_caller_unit",
        ):
            if name not in columns:
                connection.execute(
                    text(f"ALTER TABLE conversations ADD COLUMN {name} VARCHAR(120)")
                )

    async def sessions(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def dispose(self) -> None:
        await self.engine.dispose()
