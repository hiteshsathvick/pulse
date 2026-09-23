"""payment provider rename

Revision ID: 0012_payment_provider_rename
Revises: 0011_billing
Create Date: 2026-09-22

Renames subscriptions.stripe_customer_id/stripe_subscription_id to
payment_customer_id/payment_subscription_id. Confirmed with the user first:
Stripe test mode needed a real account this session couldn't set up (and a
paid/external dependency wasn't wanted for this either), so a
MockPaymentProvider (pulse/billing/providers.py) is now the default and
real Stripe an optional swap-in behind the same interface -- these columns
hold whichever provider's customer/subscription id, not necessarily
Stripe's, so their names shouldn't say "stripe" specifically. See
SPEC.md #6.18.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012_payment_provider_rename"
down_revision: str | None = "0011_billing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("subscriptions", "stripe_customer_id", new_column_name="payment_customer_id")
    op.drop_constraint("uq_subscriptions_stripe_subscription_id", "subscriptions", type_="unique")
    op.alter_column(
        "subscriptions", "stripe_subscription_id", new_column_name="payment_subscription_id"
    )
    op.create_unique_constraint(
        "uq_subscriptions_payment_subscription_id", "subscriptions", ["payment_subscription_id"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_subscriptions_payment_subscription_id", "subscriptions", type_="unique")
    op.alter_column(
        "subscriptions", "payment_subscription_id", new_column_name="stripe_subscription_id"
    )
    op.create_unique_constraint(
        "uq_subscriptions_stripe_subscription_id", "subscriptions", ["stripe_subscription_id"]
    )
    op.alter_column("subscriptions", "payment_customer_id", new_column_name="stripe_customer_id")
