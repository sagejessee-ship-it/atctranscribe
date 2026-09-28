"""Gold references in an isolated schema; per-result quality flags.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Gold never shares a schema with the transcription path (ADR-015).
    op.execute("CREATE SCHEMA IF NOT EXISTS reference")
    op.create_table(
        "gold_segment",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("dataset", sa.Text(), nullable=False),
        sa.Column("recording_id", sa.Text(), nullable=False),
        sa.Column("source_segment_index", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=False),
        sa.Column("end_ms", sa.Integer(), nullable=False),
        sa.Column("speaker", sa.Text(), nullable=True),
        sa.Column("speaker_label", sa.Text(), nullable=True),
        sa.Column("text_raw", sa.Text(), nullable=False),
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("split_source", sa.Text(), nullable=False),
        sa.Column("non_english", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "split IN ('calibration', 'test', 'train', 'dev')", name=op.f("ck_gold_segment_split")
        ),
        sa.CheckConstraint("end_ms > start_ms", name=op.f("ck_gold_segment_interval")),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["corpus_source.id"],
            name=op.f("fk_gold_segment_source_id_corpus_source"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gold_segment")),
        sa.UniqueConstraint(
            "source_id", "relative_path", name=op.f("uq_gold_segment_source_id_relative_path")
        ),
        schema="reference",
    )
    op.add_column(
        "transcription_result",
        sa.Column(
            "quality",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("transcription_result", "quality")
    op.drop_table("gold_segment", schema="reference")
    op.execute("DROP SCHEMA IF EXISTS reference")
