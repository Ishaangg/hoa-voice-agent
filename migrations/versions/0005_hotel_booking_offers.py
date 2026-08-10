"""Add durable booking offers and PMS lookup audit records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "0005_hotel_booking_offers"
down_revision = "0004_hotel_deferred_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("hotel_booking_offers"):
        op.create_table(
            "hotel_booking_offers",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("hotel_id", sa.Uuid(), nullable=False),
            sa.Column("conversation_id", sa.Uuid(), nullable=False),
            sa.Column("hold_reservation_id", sa.Uuid(), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("invalidated_reason", sa.String(length=160), nullable=True),
            sa.Column("plan_version", sa.Integer(), nullable=False),
            sa.Column("plan_json", sa.Text(), nullable=False),
            sa.Column("check_in", sa.Date(), nullable=False),
            sa.Column("check_out", sa.Date(), nullable=False),
            sa.Column("guest_count", sa.Integer(), nullable=False),
            sa.Column("room_lines_json", sa.Text(), nullable=False),
            sa.Column("nightly_total_cents", sa.Integer(), nullable=False),
            sa.Column("inventory_checked_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_hotel_booking_offers_hotel_id", "hotel_booking_offers", ["hotel_id"])
        op.create_index(
            "ix_hotel_booking_offers_conversation_id",
            "hotel_booking_offers",
            ["conversation_id"],
        )
        op.create_index("ix_hotel_booking_offers_status", "hotel_booking_offers", ["status"])
        op.create_index("ix_hotel_booking_offers_expires_at", "hotel_booking_offers", ["expires_at"])
    if not inspector.has_table("hotel_booking_tool_events"):
        op.create_table(
            "hotel_booking_tool_events",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("hotel_id", sa.Uuid(), nullable=False),
            sa.Column("conversation_id", sa.Uuid(), nullable=False),
            sa.Column("tool_name", sa.String(length=64), nullable=False),
            sa.Column("arguments_json", sa.Text(), nullable=False),
            sa.Column("result_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_hotel_booking_tool_events_hotel_id",
            "hotel_booking_tool_events",
            ["hotel_id"],
        )
        op.create_index(
            "ix_hotel_booking_tool_events_conversation_id",
            "hotel_booking_tool_events",
            ["conversation_id"],
        )
    existing = {column["name"] for column in inspector.get_columns("hotel_conversations")}
    if "booking_offer_id" not in existing:
        op.execute("ALTER TABLE hotel_conversations ADD COLUMN booking_offer_id VARCHAR(36)")


def downgrade() -> None:
    raise RuntimeError("Booking-offer migration is additive; restore a backup instead.")
