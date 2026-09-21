"""insights

Revision ID: 0008_insights
Revises: 0007_schema_registry
Create Date: 2026-09-21

RLS-protected on org_id, like event_schemas/audit_logs -- a browse-this-
project's-saved-insights pattern, not token redemption. See SPEC.md #4.1 and
#6.12.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_insights"
down_revision: str | None = "0007_schema_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_insight_kind = postgresql.ENUM(
    "TREND", "FUNNEL", "RETENTION", name="insight_kind", create_type=False
)


def upgrade() -> None:
    _insight_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "insights",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", _insight_kind, nullable=False),
        sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
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
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_insights_project_id", "insights", ["project_id"])
    op.execute("ALTER TABLE insights ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE insights FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON insights
            USING (org_id = current_setting('app.current_org_id', true)::uuid)
            WITH CHECK (org_id = current_setting('app.current_org_id', true)::uuid)
        """
    )


def downgrade() -> None:
    op.drop_table("insights")
    op.execute("DROP TYPE insight_kind")
