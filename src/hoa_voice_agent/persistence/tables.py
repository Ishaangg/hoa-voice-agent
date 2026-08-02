from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
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
    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_status: Mapped[str] = mapped_column(String(32), default="approved")
    approved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    assigned_to: Mapped[str | None] = mapped_column(String(120), nullable=True)
    assignee_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_status: Mapped[str] = mapped_column(String(16), default="unknown")
    price_shown_to_caller: Mapped[str | None] = mapped_column(Text, nullable=True)
    resident_consent: Mapped[str] = mapped_column(String(24), default="not_required")
    quote_status: Mapped[str] = mapped_column(String(24), default="not_required")
    quote_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quote_approver: Mapped[str | None] = mapped_column(String(120), nullable=True)
    final_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    billed_to: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dispatch_route_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
    )


class ManagementCompanyRecord(Base):
    __tablename__ = "management_companies"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class CommunityRecord(Base):
    __tablename__ = "communities"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    management_company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("management_companies.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    office_phone: Mapped[str] = mapped_column(String(32))
    emergency_policy_summary: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class StaffUserRecord(Base):
    __tablename__ = "staff_users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class StaffMembershipRecord(Base):
    __tablename__ = "staff_memberships"
    __table_args__ = (
        UniqueConstraint("staff_user_id", "management_company_id", name="uq_staff_company"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    staff_user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("staff_users.id"), index=True)
    management_company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("management_companies.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(32))
    community_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True, index=True)


class StaffSessionRecord(Base):
    __tablename__ = "staff_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    staff_user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("staff_users.id"), index=True)
    management_company_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class RoutingContactRecord(Base):
    __tablename__ = "routing_contacts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    management_company_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    community_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("communities.id"), index=True)
    contact_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class VendorRouteRecord(Base):
    __tablename__ = "vendor_routes"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    management_company_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    community_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("communities.id"), index=True)
    incident_type: Mapped[str] = mapped_column(String(64))
    vendor_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("vendors.id"), nullable=True, index=True
    )
    # Kept for backwards compatibility while existing installations migrate.
    vendor_name: Mapped[str] = mapped_column(String(160))
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dispatch_mode: Mapped[str] = mapped_column(String(32), default="auto_up_to_limit")
    diagnostic_fee_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    automatic_approval_limit_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payer_responsibility: Mapped[str] = mapped_column(
        String(32), default="confirm_before_booking"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class VendorRecord(Base):
    __tablename__ = "vendors"
    __table_args__ = (
        UniqueConstraint("management_company_id", "name", name="uq_vendor_company_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    management_company_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("management_companies.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PendingDispatchRecord(Base):
    """An intake that is waiting for the caller to approve a service call."""

    __tablename__ = "pending_dispatches"
    __table_args__ = (UniqueConstraint("conversation_id", name="uq_pending_dispatch_conversation"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id"), index=True
    )
    management_company_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    community_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    incident_type: Mapped[str] = mapped_column(String(64))
    priority: Mapped[str] = mapped_column(String(32))
    description: Mapped[str] = mapped_column(Text)
    route_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    price_status: Mapped[str] = mapped_column(String(16), default="unknown")
    price_disclosure: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class VendorDispatchRecord(Base):
    __tablename__ = "vendor_dispatches"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    work_order_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("work_orders.id"), index=True)
    vendor_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("vendors.id"), nullable=True)
    vendor_name: Mapped[str] = mapped_column(String(160))
    vendor_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    notification_channel: Mapped[str] = mapped_column(String(24), default="sms")
    provider_reference: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbound_reference: Mapped[str | None] = mapped_column(String(160), nullable=True)
    last_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CaseActionRecord(Base):
    __tablename__ = "case_actions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    work_order_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("work_orders.id"), index=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("staff_users.id"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class KnowledgeRevisionRecord(Base):
    __tablename__ = "knowledge_revisions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    knowledge_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("community_knowledge.id"), index=True
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("staff_users.id"), nullable=True
    )
    action_type: Mapped[str] = mapped_column(String(32))
    snapshot: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
