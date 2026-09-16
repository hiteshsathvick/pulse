"""baseline: prove the Alembic harness runs, before any real models exist

Revision ID: 0001_baseline
Revises:
Create Date: 2026-09-16

Control-plane models (User/Organization/Membership/Project) land in Phase 2
via autogenerate against real SQLAlchemy models. This revision exists only
to give Phase 1's up/down round-trip something real to exercise.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "_pulse_schema_baseline",
        sa.Column("id", sa.Integer(), primary_key=True),
    )


def downgrade() -> None:
    op.drop_table("_pulse_schema_baseline")
