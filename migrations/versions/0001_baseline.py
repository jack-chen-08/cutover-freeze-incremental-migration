"""Baseline member-space schema.

The DDL lives in migrations/sql/0001_schema.sql so the drill script, Alembic and a manual
psql session all execute byte-identical statements.
"""
from __future__ import annotations

from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

SQL_FILE = Path(__file__).resolve().parents[1] / "sql" / "0001_schema.sql"


def upgrade() -> None:
    op.execute(SQL_FILE.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS migration_state, migration_batches, kb_articles, opportunities, "
        "registrations, activities, tag_links, tags, contacts, member_companies, user_roles, "
        "users, roles, sites, orgs CASCADE"
    )
