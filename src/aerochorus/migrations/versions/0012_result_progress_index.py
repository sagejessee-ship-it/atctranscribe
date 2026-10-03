"""Index results by model run and time, for live progress and stall detection.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_transcription_result_srm_created",
        "transcription_result",
        ["sweep_run_model_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_transcription_result_srm_created", table_name="transcription_result")
