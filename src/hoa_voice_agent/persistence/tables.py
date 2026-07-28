from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
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
    pending_caller_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    pending_caller_unit: Mapped[str | None] = mapped_column(String(120), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="ready")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class ConversationTurnRecord(Base):
    __tablename__ = "conversation_turns"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("conversations.id"),
        index=True,
    )
    speaker: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class CallerProfileRecord(Base):
    """Caller-provided context, kept separate from an authoritative resident directory."""

    __tablename__ = "caller_profiles"
    __table_args__ = (
        UniqueConstraint("community_id", "caller_phone", name="uq_caller_profile_community_phone"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    management_company_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    community_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    caller_phone: Mapped[str] = mapped_column(String(32))
    claimed_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    claimed_unit: Mapped[str | None] = mapped_column(String(120), nullable=True)
    identity_status: Mapped[str] = mapped_column(String(32), default="unverified")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class CommunityKnowledgeRecord(Base):
    """An approved, community-scoped fact the agent may use in a response."""

    __tablename__ = "community_knowledge"
    __table_args__ = (
        UniqueConstraint("community_id", "topic", name="uq_community_knowledge_topic"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    management_company_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    community_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    category: Mapped[str] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(String(120))
    answer: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class CallerProfileHistoryRecord(Base):
    __tablename__ = "caller_profile_history"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    caller_profile_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("caller_profiles.id"),
        index=True,
    )
    field_name: Mapped[str] = mapped_column(String(32))
    old_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    new_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    change_source: Mapped[str] = mapped_column(String(32), default="conversation")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class WorkflowEventRecord(Base):
    __tablename__ = "workflow_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("conversations.id"),
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    detail: Mapped[str] = mapped_column(Text)
    reference_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
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
