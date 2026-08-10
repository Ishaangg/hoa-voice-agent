"""Baseline schema for new production installations."""

from alembic import op

from hoa_voice_agent.persistence import hotel_tables as _hotel_tables  # noqa: F401
from hoa_voice_agent.persistence import tables as _tables  # noqa: F401
from hoa_voice_agent.persistence.database import Base

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This is the baseline for the existing pilot schema. It creates all
    # current tables for a new database and is harmless when tables already
    # exist, allowing a pilot database to be adopted without data loss.
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    raise RuntimeError("The baseline migration is not reversible; restore a backup instead.")
