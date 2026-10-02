"""Retire persisted AI chat after a verified operator backup."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import context, op

revision = "0046_issue166_retire_ai_history"
down_revision = "0045_issue151_ai_conversations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Freeze the retired tables before checking so a legacy writer cannot
    # create history between the empty check and DROP. No other table changes.
    bind = op.get_bind()
    bind.execute(
        sa.text("LOCK TABLE ai_conversations, ai_conversation_messages IN ACCESS EXCLUSIVE MODE")
    )
    has_history = bind.scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM ai_conversations) OR "
            "EXISTS (SELECT 1 FROM ai_conversation_messages)"
        )
    )
    if (
        has_history
        and context.get_x_argument(as_dictionary=True).get("ai_history_backup_verified") != "true"
    ):
        raise RuntimeError(
            "AI history exists. Stop writers, back up and verify the database, then run "
            "alembic -x ai_history_backup_verified=true upgrade head. "
            "This retires only the two AI chat tables."
        )
    op.drop_table("ai_conversation_messages")
    op.drop_table("ai_conversations")


def downgrade() -> None:
    # Recreate the historical schema only. Content recovery requires an
    # explicit scoped backup restore; downgrade never manufactures history.
    path = Path(__file__).with_name("0045_issue151_ai_conversations.py")
    spec = importlib.util.spec_from_file_location("issue151_schema", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.upgrade()
