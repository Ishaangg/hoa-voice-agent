from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from hoa_voice_agent.persistence.database import Base


class ConversationRecord(Base):
    __tablename__ = "conversations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    management_company_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    community_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    resident_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    caller_phone: Mapped[str] = mapped_column(String(32))
    caller_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    caller_unit: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="ready")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class WorkOrderRecord(Base):
    __tablename__ = "work_orders"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("conversations.id"),
        index=True,
    )
    management_company_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    community_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    resident_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    incident_type: Mapped[str] = mapped_column(String(64))
    priority: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )
