"""billing

Revision ID: 0011_billing
Revises: 0010_alerts
Create Date: 2026-09-22

Both tables are RLS-protected on org_id, like alerts/alert_events. See
SPEC.md #4.1 and #6.17.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_billing"
down_revision: str | None = "0010_alerts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_subscription_plan = postgresql.ENUM("FREE", "PRO", name="subscription_plan", create_type=False)
_subscription_status = postgresql.ENUM(
    "ACTIVE", "PAST_DUE", "CANCELED", "INCOMPLETE", name="subscription_status", create_type=False
)

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
    _subscription_plan.create(op.get_bind(), checkfirst=True)
    _subscription_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "subscriptions",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("plan", _subscription_plan, nullable=False),
        sa.Column("status", _subscription_status, nullable=False),
        sa.Column("stripe_customer_id", sa.String(), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(), nullable=True),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", name="uq_subscriptions_org_id"),
        sa.UniqueConstraint(
            "stripe_subscription_id", name="uq_subscriptions_stripe_subscription_id"
        ),
    )
    op.execute("ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE subscriptions FORCE ROW LEVEL SECURITY")
    op.execute(_POLICY.format(table="subscriptions"))

    op.create_table(
        "usage_records",
        sa.Column("org_id", sa.UUID(), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("events_ingested", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mtu", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "period", name="uq_usage_records_org_period"),
    )
    op.create_index("ix_usage_records_org_id", "usage_records", ["org_id"])
    op.execute("ALTER TABLE usage_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE usage_records FORCE ROW LEVEL SECURITY")
    op.execute(_POLICY.format(table="usage_records"))


def downgrade() -> None:
    op.drop_table("usage_records")
    op.drop_table("subscriptions")
    op.execute("DROP TYPE subscription_status")
    op.execute("DROP TYPE subscription_plan")
