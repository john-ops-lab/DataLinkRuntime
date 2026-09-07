"""Remove retired portable fields and consolidate descriptions."""

from collections.abc import Sequence

from alembic import op

revision: str = "0037_portable_simplified"
down_revision: str | None = "0036_portable"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE user_templates
        SET content = (content - ARRAY['instructions', 'tags', 'example_files'])
            || jsonb_build_object(
                'description', CASE
                    WHEN COALESCE(content->>'instructions', '') = ''
                        OR content->>'instructions' = content->>'description'
                    THEN content->>'description'
                    ELSE concat_ws(E'\n\n', NULLIF(content->>'description', ''),
                                   content->>'instructions')
                END,
                'variants', (
                    SELECT jsonb_agg(
                        variant - ARRAY['required_parameters', 'input_skeleton', 'output_example']
                        ORDER BY position
                    )
                    FROM jsonb_array_elements(content->'variants')
                        WITH ORDINALITY AS variants(variant, position)
                )
            ),
            version = version + 1,
            updated_at = now()
        """
    )
    op.execute(
        """
        UPDATE adapters
        SET description = CASE
                WHEN COALESCE(configuration_notes->>'instructions', '') = ''
                    OR configuration_notes->>'instructions' = description
                THEN description
                ELSE concat_ws(E'\n\n', NULLIF(description, ''),
                               configuration_notes->>'instructions')
            END,
            configuration_notes = configuration_notes - ARRAY['instructions', 'required_parameters']
        WHERE configuration_notes ?| ARRAY['instructions', 'required_parameters']
        """
    )


def downgrade() -> None:
    # Removed examples and labels are intentionally not reconstructed.
    # Restore a pre-upgrade backup if the previous content format is needed.
    pass
