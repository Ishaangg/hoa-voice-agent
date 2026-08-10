"""Persist caller booking-fact provenance and correction state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0007_hotel_booking_fact_lifecycle"
down_revision = "0006_hotel_room_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("hotel_conversations")}
    if "booking_facts_json" not in existing:
        op.add_column("hotel_conversations", sa.Column("booking_facts_json", sa.Text()))
    if "booking_fact_revision" not in existing:
        op.add_column(
            "hotel_conversations",
            sa.Column("booking_fact_revision", sa.Integer(), nullable=False, server_default="0"),
        )
    if "booking_nights" not in existing:
        op.add_column("hotel_conversations", sa.Column("booking_nights", sa.Integer()))


def downgrade() -> None:
    raise RuntimeError("Booking fact lifecycle migration is additive; restore a backup instead.")
