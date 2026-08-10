"""Add one bounded suspended workflow to hotel conversations."""

from alembic import op
from sqlalchemy import inspect

revision = "0004_hotel_deferred_workflow"
down_revision = "0003_hotel_dialogue_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = {column["name"] for column in inspector.get_columns("hotel_conversations")}
    additions = {
        "deferred_category": "VARCHAR(64)",
        "deferred_description": "TEXT",
        "deferred_state": "VARCHAR(64)",
        "deferred_turns": "INTEGER DEFAULT 0",
    }
    for name, column_type in additions.items():
        if name not in existing:
            op.execute(f"ALTER TABLE hotel_conversations ADD COLUMN {name} {column_type}")


def downgrade() -> None:
    raise RuntimeError("The deferred-workflow migration is additive; restore a backup instead.")
