"""Embed each item's subject on its own, to tell whether two items answer the same question.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Filled for new items on insert; existing items are backfilled lazily when a commit or
    # merge first compares against them.
    op.execute("ALTER TABLE state_items ADD COLUMN IF NOT EXISTS subject_embedding vector(768)")


def downgrade() -> None:
    op.execute("ALTER TABLE state_items DROP COLUMN IF EXISTS subject_embedding")
