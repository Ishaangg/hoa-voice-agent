"""Persistence models for the hotel vertical.

These tables intentionally do not reuse HOA conversations, residents, work
orders, communities, vendors, or knowledge records.  The shared platform
layer may reuse staff authentication and messaging adapters, but the hotel
operational data has its own lifecycle and vocabulary.
"""

# ruff: noqa: E501

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from hoa_voice_agent.persistence.database import Base


class HotelOrganizationRecord(Base):
    __tablename__ = "hotel_organizations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class HotelRecord(Base):
    __tablename__ = "hotels"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    organization_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotel_organizations.id"), index=True
    )
    name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    emergency_policy_summary: Mapped[str] = mapped_column(
        Text, default="Contact the front desk for immediate safety concerns."
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class HotelDepartmentRecord(Base):
    __tablename__ = "hotel_departments"
    __table_args__ = (UniqueConstraint("hotel_id", "key", name="uq_hotel_department_key"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    key: Mapped[str] = mapped_column(String(48))
    name: Mapped[str] = mapped_column(String(120))
    escalation_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class HotelRoomRecord(Base):
    __tablename__ = "hotel_rooms"
    __table_args__ = (UniqueConstraint("hotel_id", "room_number", name="uq_hotel_room_number"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    room_number: Mapped[str] = mapped_column(String(16))
    room_type: Mapped[str] = mapped_column(String(64))
    max_occupancy: Mapped[int] = mapped_column(Integer, default=2)
    nightly_rate_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="available")


class HotelCatalogVersionRecord(Base):
    """The authoritative version of a hotel's room catalogue."""

    __tablename__ = "hotel_catalog_versions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotels.id"), unique=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class HotelRoomTypeRecord(Base):
    """Configured room-type metadata shared by the physical inventory."""

    __tablename__ = "hotel_room_types"
    __table_args__ = (UniqueConstraint("hotel_id", "key", name="uq_hotel_room_type_key"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    display_name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    bed_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    max_occupancy: Mapped[int] = mapped_column(Integer, default=2)
    accessibility_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    policies: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class HotelAmenityRecord(Base):
    """A hotel-defined amenity that can be attached to room types."""

    __tablename__ = "hotel_amenities"
    __table_args__ = (UniqueConstraint("hotel_id", "key", name="uq_hotel_amenity_key"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    category: Mapped[str] = mapped_column(String(64), default="amenity")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class HotelRoomTypeAmenityRecord(Base):
    """Many-to-many mapping between configured room types and amenities."""

    __tablename__ = "hotel_room_type_amenities"
    __table_args__ = (
        UniqueConstraint(
            "room_type_id", "amenity_id", name="uq_hotel_room_type_amenity"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    room_type_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotel_room_types.id"), index=True
    )
    amenity_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotel_amenities.id"), index=True
    )


class HotelRatePlanRecord(Base):
    """A price and included-service package for one room type."""

    __tablename__ = "hotel_rate_plans"
    __table_args__ = (
        UniqueConstraint("room_type_id", "code", name="uq_hotel_rate_plan_code"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    room_type_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotel_room_types.id"), index=True
    )
    code: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    nightly_rate_cents: Mapped[int] = mapped_column(Integer, default=0)
    included_meals_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancellation_policy: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class HotelGuestRecord(Base):
    __tablename__ = "hotel_guests"
    __table_args__ = (UniqueConstraint("hotel_id", "phone", name="uq_hotel_guest_phone"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    full_name: Mapped[str] = mapped_column(String(160))
    phone: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class HotelReservationRecord(Base):
    __tablename__ = "hotel_reservations"
    __table_args__ = (
        UniqueConstraint("hotel_id", "confirmation_code", name="uq_hotel_confirmation_code"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    guest_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("hotel_guests.id"), nullable=True, index=True
    )
    room_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("hotel_rooms.id"), nullable=True, index=True
    )
    confirmation_code: Mapped[str] = mapped_column(String(32))
    check_in: Mapped[date] = mapped_column(Date)
    check_out: Mapped[date] = mapped_column(Date)
    guest_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="confirmed")
    payment_status: Mapped[str] = mapped_column(String(32), default="demo_confirmed")
    # A hold is a reservation-shaped aggregate before it becomes a guest's
    # confirmed stay.  Availability is determined from these dated records,
    # never by permanently changing HotelRoomRecord.status to "held".
    hold_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    booking_plan_version: Mapped[int] = mapped_column(Integer, default=0)
    nightly_total_cents: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class HotelReservationRoomRecord(Base):
    """One physical-room assignment inside a dated hotel reservation."""

    __tablename__ = "hotel_reservation_rooms"
    __table_args__ = (
        UniqueConstraint(
            "reservation_id", "room_id", name="uq_hotel_reservation_room_assignment"
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    reservation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotel_reservations.id"), index=True
    )
    room_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotel_rooms.id"), index=True)
    room_type: Mapped[str] = mapped_column(String(64))
    guest_count: Mapped[int] = mapped_column(Integer, default=1)
    nightly_rate_cents: Mapped[int] = mapped_column(Integer, default=0)


class HotelKnowledgeRecord(Base):
    __tablename__ = "hotel_knowledge"
    __table_args__ = (UniqueConstraint("hotel_id", "topic", name="uq_hotel_knowledge_topic"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    category: Mapped[str] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(String(120))
    answer: Mapped[str] = mapped_column(Text)
    source_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_status: Mapped[str] = mapped_column(String(32), default="approved")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class HotelMenuItemRecord(Base):
    """A hotel-managed item that the voice agent may offer for room service."""

    __tablename__ = "hotel_menu_items"
    __table_args__ = (
        UniqueConstraint("hotel_id", "name", name="uq_hotel_menu_item_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    category: Mapped[str] = mapped_column(String(64), default="Menu")
    name: Mapped[str] = mapped_column(String(160))
    aliases: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_available: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class HotelConversationRecord(Base):
    __tablename__ = "hotel_conversations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    caller_phone: Mapped[str] = mapped_column(String(32))
    caller_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    room_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    guest_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("hotel_guests.id"), nullable=True, index=True
    )
    reservation_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("hotel_reservations.id"), nullable=True, index=True
    )
    verification_status: Mapped[str] = mapped_column(String(32), default="unverified")
    state: Mapped[str] = mapped_column(String(32), default="ready")
    active_topic: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_turn_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    pending_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pending_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # One bounded suspended workflow prevents a caller's second request from
    # silently overwriting unfinished booking or service context.
    deferred_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deferred_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    deferred_state: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deferred_turns: Mapped[int] = mapped_column(Integer, default=0)
    booking_check_in: Mapped[date | None] = mapped_column(Date, nullable=True)
    booking_check_out: Mapped[date | None] = mapped_column(Date, nullable=True)
    booking_guest_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    booking_nights: Mapped[int | None] = mapped_column(Integer, nullable=True)
    booking_room_preference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    booking_last_search_result: Mapped[str | None] = mapped_column(Text, nullable=True)
    booking_catalog_snapshot_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    booking_catalog_relevant_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    booking_criteria_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    booking_criteria_revision: Mapped[int] = mapped_column(Integer, default=0)
    # The scalar booking columns below remain projections for existing PMS
    # code.  This ledger preserves provenance and correction state so a newer
    # caller value cannot silently replace an active one.
    booking_facts_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    booking_fact_revision: Mapped[int] = mapped_column(Integer, default=0)
    booking_response_scope: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # The typed plan preserves customer-selected room groups separately from
    # any system recommendation or historical transcript text.
    booking_plan_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    booking_plan_version: Mapped[int] = mapped_column(Integer, default=0)
    # Kept separate from reservation_id: an in-progress caller might also be
    # an existing guest with a confirmed reservation.
    booking_hold_reservation_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("hotel_reservations.id"), nullable=True, index=True
    )
    # The currently selectable quote.  It is separate from a hold because a
    # quote reserves nothing until the caller explicitly accepts it.
    booking_offer_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("hotel_booking_offers.id"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class HotelConversationTurnRecord(Base):
    __tablename__ = "hotel_conversation_turns"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotel_conversations.id"), index=True
    )
    speaker: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class HotelBookingOfferRecord(Base):
    """An immutable, expiring booking option shown to one caller.

    A quote is not an allocation.  It records the inventory facts and price
    that were shown so accepting it can be safely revalidated before a hold is
    created.
    """

    __tablename__ = "hotel_booking_offers"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotel_conversations.id"), index=True
    )
    hold_reservation_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("hotel_reservations.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), default="presented", index=True)
    invalidated_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)
    plan_version: Mapped[int] = mapped_column(Integer)
    plan_json: Mapped[str] = mapped_column(Text)
    check_in: Mapped[date] = mapped_column(Date)
    check_out: Mapped[date] = mapped_column(Date)
    guest_count: Mapped[int] = mapped_column(Integer)
    room_lines_json: Mapped[str] = mapped_column(Text)
    nightly_total_cents: Mapped[int] = mapped_column(Integer)
    inventory_checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class HotelBookingToolEventRecord(Base):
    """Audit evidence for a PMS fact used during a booking conversation."""

    __tablename__ = "hotel_booking_tool_events"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotel_conversations.id"), index=True
    )
    tool_name: Mapped[str] = mapped_column(String(64))
    arguments_json: Mapped[str] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class HotelRequestRecord(Base):
    __tablename__ = "hotel_requests"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotel_conversations.id"), index=True
    )
    guest_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("hotel_guests.id"), nullable=True, index=True
    )
    reservation_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("hotel_reservations.id"), nullable=True, index=True
    )
    department_id: Mapped[UUID | None] = mapped_column(
        Uuid, ForeignKey("hotel_departments.id"), nullable=True, index=True
    )
    category: Mapped[str] = mapped_column(String(64))
    priority: Mapped[str] = mapped_column(String(24), default="routine")
    description: Mapped[str] = mapped_column(Text)
    room_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="open")
    assigned_to: Mapped[str | None] = mapped_column(String(120), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    notification_status: Mapped[str] = mapped_column(String(24), default="not_sent")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class HotelRequestActionRecord(Base):
    __tablename__ = "hotel_request_actions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    request_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotel_requests.id"), index=True)
    actor: Mapped[str] = mapped_column(String(120))
    action_type: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class HotelStaffMembershipRecord(Base):
    """Hotel workspace membership; HOA staff memberships remain separate."""

    __tablename__ = "hotel_staff_memberships"
    __table_args__ = (
        UniqueConstraint("staff_user_id", "organization_id", name="uq_hotel_staff_membership"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    staff_user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("hotel_staff_users.id"),
        index=True,
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotel_organizations.id"), index=True
    )
    role: Mapped[str] = mapped_column(String(32), default="hotel_manager")
    hotel_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("hotels.id"), nullable=True)


class HotelSessionRecord(Base):
    """Separate cookie session table so hotel and HOA workspaces cannot mix."""

    __tablename__ = "hotel_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    staff_user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("hotel_staff_users.id"),
        index=True,
    )
    organization_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("hotel_organizations.id"), index=True
    )
    hotel_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("hotels.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class HotelStaffUserRecord(Base):
    __tablename__ = "hotel_staff_users"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(160))
    password_hash: Mapped[str] = mapped_column(String(256))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
