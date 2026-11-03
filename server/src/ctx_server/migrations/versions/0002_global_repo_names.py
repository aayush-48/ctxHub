"""Repo names unique across all owners; drop the removed import feature's tables.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    clashes = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT lower(name) AS n, count(*) AS c FROM repos GROUP BY lower(name) "
                "HAVING count(*) > 1"
            )
        )
        .all()
    )
    if clashes:
        names = ", ".join(f"{r.n} ({r.c})" for r in clashes)
        raise RuntimeError(
            "repo names must become globally unique, but these names are used more than "
            f"once: {names}. Rename or delete the duplicates, then deploy again."
        )
    op.execute("ALTER TABLE repos DROP CONSTRAINT IF EXISTS repos_owner_id_name_key")
    op.execute("CREATE UNIQUE INDEX ux_repos_name ON repos (lower(name))")
    # Server-side export import (and its background jobs) was removed: the user's chat model
    # now reads exports and commits typed items itself.
    op.execute("DROP TABLE IF EXISTS uploads")
    op.execute("DROP TABLE IF EXISTS jobs")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_repos_name")
    op.execute("ALTER TABLE repos ADD CONSTRAINT repos_owner_id_name_key UNIQUE (owner_id, name)")
