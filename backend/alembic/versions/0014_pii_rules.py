"""pii rules

Revision ID: 0014_pii_rules
Revises: 0013_project_retention_days
Create Date: 2026-09-23

Phase 22: a proactive, project-level PII control -- a list of property
KEYS (not tied to one event name) with an action (hash or drop), checked
at ingest before a property is even known to the schema registry. Chosen
over enforcing the existing PropertySchema.is_pii flag (Phase 9) because
that flag is inherently reactive: it only protects a property after an
event carrying it has already been ingested and registered once
unprotected, and it's scoped per event name, so the same property key on
a different event would need marking again. is_pii stays as-is --
descriptive/informational only, not repurposed. RLS-protected on org_id,
same pattern as every other tenant table. See SPEC.md #6.20.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_pii_rules"
down_revision: str | None = "0013_project_retention_days"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_pii_action = postgresql.ENUM("HASH", "DROP", name="pii_action", create_type=False)

_POLICY = """
    CREATE POLICY tenant_isolation ON pii_rules
        USING (org_id = current_setting('app.current_org_id', true)::uuid)
        WITH CHECK (org_id = current_setting('app.current_org_id', true)::uuid)
"""


def upgrade() -> None:
    _pii_action.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "pii_rules",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("property_key", sa.String(), nullable=False),
        sa.Column("action", _pii_action, nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "property_key", name="uq_pii_rules_project_property"),
    )
    op.create_index("ix_pii_rules_org_id", "pii_rules", ["org_id"])
    op.execute("ALTER TABLE pii_rules ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE pii_rules FORCE ROW LEVEL SECURITY")
    op.execute(_POLICY)


def downgrade() -> None:
    op.drop_table("pii_rules")
    op.execute("DROP TYPE pii_action")
