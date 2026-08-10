"""Add persisted room metadata and booking criteria state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0006_hotel_room_catalog"
down_revision = "0005_hotel_booking_offers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())

    if not inspector.has_table("hotel_catalog_versions"):
        op.create_table(
            "hotel_catalog_versions",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("hotel_id", sa.Uuid(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["hotel_id"], ["hotels.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("hotel_id"),
        )
        op.create_index(
            "ix_hotel_catalog_versions_hotel_id",
            "hotel_catalog_versions",
            ["hotel_id"],
        )

    if not inspector.has_table("hotel_room_types"):
        op.create_table(
            "hotel_room_types",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("hotel_id", sa.Uuid(), nullable=False),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("display_name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("bed_type", sa.String(length=64), nullable=True),
            sa.Column("max_occupancy", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("accessibility_notes", sa.Text(), nullable=True),
            sa.Column("policies", sa.Text(), nullable=True),
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["hotel_id"], ["hotels.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("hotel_id", "key", name="uq_hotel_room_type_key"),
        )
        op.create_index("ix_hotel_room_types_hotel_id", "hotel_room_types", ["hotel_id"])

    if not inspector.has_table("hotel_amenities"):
        op.create_table(
            "hotel_amenities",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("hotel_id", sa.Uuid(), nullable=False),
            sa.Column("key", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("category", sa.String(length=64), nullable=False, server_default="amenity"),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.ForeignKeyConstraint(["hotel_id"], ["hotels.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("hotel_id", "key", name="uq_hotel_amenity_key"),
        )
        op.create_index("ix_hotel_amenities_hotel_id", "hotel_amenities", ["hotel_id"])

    if not inspector.has_table("hotel_room_type_amenities"):
        op.create_table(
            "hotel_room_type_amenities",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("room_type_id", sa.Uuid(), nullable=False),
            sa.Column("amenity_id", sa.Uuid(), nullable=False),
            sa.ForeignKeyConstraint(["room_type_id"], ["hotel_room_types.id"]),
            sa.ForeignKeyConstraint(["amenity_id"], ["hotel_amenities.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "room_type_id", "amenity_id", name="uq_hotel_room_type_amenity"
            ),
        )
        op.create_index(
            "ix_hotel_room_type_amenities_room_type_id",
            "hotel_room_type_amenities",
            ["room_type_id"],
        )
        op.create_index(
            "ix_hotel_room_type_amenities_amenity_id",
            "hotel_room_type_amenities",
            ["amenity_id"],
        )

    if not inspector.has_table("hotel_rate_plans"):
        op.create_table(
            "hotel_rate_plans",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("hotel_id", sa.Uuid(), nullable=False),
            sa.Column("room_type_id", sa.Uuid(), nullable=False),
            sa.Column("code", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("nightly_rate_cents", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("included_meals_json", sa.Text(), nullable=True),
            sa.Column("cancellation_policy", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["hotel_id"], ["hotels.id"]),
            sa.ForeignKeyConstraint(["room_type_id"], ["hotel_room_types.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("room_type_id", "code", name="uq_hotel_rate_plan_code"),
        )
        op.create_index("ix_hotel_rate_plans_hotel_id", "hotel_rate_plans", ["hotel_id"])
        op.create_index(
            "ix_hotel_rate_plans_room_type_id", "hotel_rate_plans", ["room_type_id"]
        )

    existing = {column["name"] for column in inspector.get_columns("hotel_conversations")}
    additions = {
        "booking_catalog_snapshot_version": "INTEGER",
        "booking_catalog_relevant_json": "TEXT",
        "booking_criteria_json": "TEXT",
        "booking_criteria_revision": "INTEGER DEFAULT 0",
        "booking_response_scope": "VARCHAR(32)",
    }
    for name, column_type in additions.items():
        if name not in existing:
            op.execute(f"ALTER TABLE hotel_conversations ADD COLUMN {name} {column_type}")


def downgrade() -> None:
    raise RuntimeError("Room-catalog migration is additive; restore a backup instead.")
