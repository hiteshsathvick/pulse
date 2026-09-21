"""dashboards

Revision ID: 0009_dashboards
Revises: 0008_insights
Create Date: 2026-09-21

Both tables are RLS-protected on org_id, like insights/event_schemas.
dashboard_items carries its own org_id (denormalized from its dashboard) so
its policy is a direct org_id check, never a join. See SPEC.md #4.1 and #6.13.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_dashboards"
down_revision: str | None = "0008_insights"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_dashboard_scope = postgresql.ENUM("PRIVATE", "ORG", name="dashboard_scope", create_type=False)

_POLICY = """
    CREATE POLICY tenant_isolation ON {table}
        USING (org_id = current_setting('app.current_org_id', true)::uuid)
        WITH CHECK (org_id = current_setting('app.current_org_id', true)::uuid)
"""


def _timestamps() -> list[sa.Column]:  # type: ignore[type-arg]
    return [
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
    ]


def upgrade() -> None:
    _dashboard_scope.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "dashboards",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("layout", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("default_range", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("shared_scope", _dashboard_scope, nullable=False),
        sa.Column("created_by", sa.UUID(), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dashboards_project_id", "dashboards", ["project_id"])
    op.execute("ALTER TABLE dashboards ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE dashboards FORCE ROW LEVEL SECURITY")
    op.execute(_POLICY.format(table="dashboards"))

    op.create_table(
        "dashboard_items",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("dashboard_id", sa.UUID(), nullable=False),
        sa.Column("insight_id", sa.UUID(), nullable=False),
        sa.Column("position", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["dashboard_id"], ["dashboards.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["insight_id"], ["insights.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dashboard_id", "insight_id", name="uq_dashboard_items_dashboard_insight"
        ),
    )
    op.create_index("ix_dashboard_items_dashboard_id", "dashboard_items", ["dashboard_id"])
    op.execute("ALTER TABLE dashboard_items ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE dashboard_items FORCE ROW LEVEL SECURITY")
    op.execute(_POLICY.format(table="dashboard_items"))


def downgrade() -> None:
    op.drop_table("dashboard_items")
    op.drop_table("dashboards")
    op.execute("DROP TYPE dashboard_scope")
