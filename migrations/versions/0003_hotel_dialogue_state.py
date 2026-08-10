"""Persist the hotel dialogue state used by the voice workflow."""

from alembic import op
from sqlalchemy import inspect

revision = "0003_hotel_dialogue_state"
down_revision = "0002_hotel_menu_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("hotel_conversations")}
    additions = {
        "active_topic": "VARCHAR(64)",
        "last_intent": "VARCHAR(64)",
        "last_turn_confidence": "FLOAT",
    }
    for name, column_type in additions.items():
        if name not in existing:
            op.execute(f"ALTER TABLE hotel_conversations ADD COLUMN {name} {column_type}")


def downgrade() -> None:
    raise RuntimeError("The dialogue-state migration is additive; restore a backup instead.")
