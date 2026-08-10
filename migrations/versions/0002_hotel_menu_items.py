"""Add the hotel-managed room-service menu."""

import sqlalchemy as sa
from alembic import op

revision = "0002_hotel_menu_items"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The pilot baseline uses ``Base.metadata.create_all`` and therefore may
    # already contain this additive table on a brand-new installation. The
    # guard keeps the migration safe for both fresh and already-adopted DBs.
    if sa.inspect(op.get_bind()).has_table("hotel_menu_items"):
        return
    op.create_table(
        "hotel_menu_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("hotel_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("aliases", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("price_cents", sa.Integer(), nullable=True),
        sa.Column("is_available", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["hotel_id"], ["hotels.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hotel_id", "name", name="uq_hotel_menu_item_name"),
    )
    op.create_index(
        "ix_hotel_menu_items_hotel_id", "hotel_menu_items", ["hotel_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_hotel_menu_items_hotel_id", table_name="hotel_menu_items")
    op.drop_table("hotel_menu_items")
