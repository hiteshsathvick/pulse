"""project retention_days

Revision ID: 0013_project_retention_days
Revises: 0012_payment_provider_rename
Create Date: 2026-09-23

Phase 22: per-project data retention. Nullable -- null means "use
Organization.retention_days" (already existed, unused until now); a
project only gets its own row here once someone actually overrides the
org default. Enforced by a new retention-worker (pulse/retention/), not
native ClickHouse TTL: TTL expressions are static per table, and a
per-project cutoff that changes whenever a setting changes is a better
fit for a scheduled DELETE mutation than for rebuilding a table-wide TTL
expression on every settings change. See SPEC.md #6.20.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_project_retention_days"
down_revision: str | None = "0012_payment_provider_rename"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("retention_days", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "retention_days")
