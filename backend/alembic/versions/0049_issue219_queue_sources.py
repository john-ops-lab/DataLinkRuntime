"""Bounded source identifiers; availability stays in the closed application registry."""

import sqlalchemy as sa
from alembic import op

revision = "0049_issue219_queue_sources"
down_revision = "0048_issue219_kubernetes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_subscription_source", "event_subscriptions", type_="check")
    op.alter_column("event_subscriptions", "source_type", type_=sa.String(32))
    op.create_check_constraint(
        "ck_subscription_source", "event_subscriptions", "source_type ~ '^[a-z][a-z0-9_]{0,31}$'"
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM event_subscriptions "
            "WHERE source_type NOT IN ('kafka','kubernetes'))"
        )
    ):
        raise RuntimeError("Preserve queue subscription responsibilities before reverting software")
    op.drop_constraint("ck_subscription_source", "event_subscriptions", type_="check")
    op.alter_column("event_subscriptions", "source_type", type_=sa.String(16))
    op.create_check_constraint(
        "ck_subscription_source", "event_subscriptions", "source_type IN ('kafka','kubernetes')"
    )
