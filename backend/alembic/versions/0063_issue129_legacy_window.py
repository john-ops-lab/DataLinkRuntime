"""Store the one-time, bounded v3 upgrade window, defaulting to closed."""

from alembic import op

revision = "0063_issue129_legacy_window"
down_revision = "0059_issue129_instance_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE worker_legacy_window (
            id SMALLINT PRIMARY KEY CHECK (id = 1),
            opened_at TIMESTAMPTZ,
            deadline_at TIMESTAMPTZ,
            closed_at TIMESTAMPTZ,
            CHECK ((opened_at IS NULL AND deadline_at IS NULL AND closed_at IS NULL) OR
                   (opened_at IS NOT NULL AND deadline_at IS NOT NULL
                    AND deadline_at > opened_at AND deadline_at <= opened_at + INTERVAL '7 days'
                    AND (closed_at IS NULL OR closed_at >= opened_at)))
        )
    """)
    op.execute("INSERT INTO worker_legacy_window (id) VALUES (1)")


def downgrade() -> None:
    # Exclude new guarded state before checking it and removing its storage.
    op.execute("LOCK TABLE worker_legacy_window IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM worker_legacy_window WHERE opened_at IS NOT NULL) THEN
                RAISE EXCEPTION 'legacy window history prevents downgrade';
            END IF;
        END $$
    """)
    op.drop_table("worker_legacy_window")
