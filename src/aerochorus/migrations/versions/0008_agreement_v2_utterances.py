"""Agreement v2: word timings on results; utterance agreement and short-text counts.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "segment_agreement",
        sa.Column(
            "representative_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "segment_agreement",
        sa.Column(
            "representative_content_tokens",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "segment_agreement",
        sa.Column(
            "utterances",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "segment_agreement",
        sa.Column("utterance_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "segment_agreement",
        sa.Column(
            "best_utterance_family_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.add_column(
        "segment_agreement",
        sa.Column(
            "best_utterance_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
    )
    op.create_index(
        "ix_segment_agreement_utterance_families",
        "segment_agreement",
        ["best_utterance_family_count"],
        unique=False,
    )
    op.add_column(
        "transcription_result",
        sa.Column("words", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transcription_result", "words")
    op.drop_index("ix_segment_agreement_utterance_families", table_name="segment_agreement")
    op.drop_column("segment_agreement", "best_utterance_tokens")
    op.drop_column("segment_agreement", "best_utterance_family_count")
    op.drop_column("segment_agreement", "utterance_count")
    op.drop_column("segment_agreement", "utterances")
    op.drop_column("segment_agreement", "representative_content_tokens")
    op.drop_column("segment_agreement", "representative_tokens")
