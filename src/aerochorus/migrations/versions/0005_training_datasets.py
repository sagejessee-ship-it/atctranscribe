"""Training datasets: frozen, versioned annotation selections with export records.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "training_dataset",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'frozen'"), nullable=False),
        sa.Column("definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("definition_sha256", sa.Text(), nullable=False),
        sa.Column("manifest_sha256", sa.Text(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("audio_ms_total", sa.BigInteger(), nullable=False),
        sa.Column(
            "counts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("export", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "manifest_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_training_dataset_manifest_sha256_format"),
        ),
        sa.CheckConstraint(
            "status IN ('frozen', 'exported')", name=op.f("ck_training_dataset_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_training_dataset")),
        sa.UniqueConstraint("name", "version", name=op.f("uq_training_dataset_name_version")),
    )
    op.create_table(
        "training_dataset_item",
        sa.Column("dataset_id", sa.BigInteger(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("annotation_version_id", sa.BigInteger(), nullable=False),
        sa.Column("segment_id", sa.BigInteger(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=True),
        sa.Column("end_ms", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("training_label", sa.Text(), nullable=False),
        sa.Column("text_origin", sa.Text(), nullable=True),
        sa.Column("source_sha256", sa.Text(), nullable=True),
        sa.Column("split", sa.Text(), nullable=False),
        sa.Column("split_group", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("clip_path", sa.Text(), nullable=True),
        sa.Column("clip_sha256", sa.Text(), nullable=True),
        sa.Column("clip_bytes", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "scope IN ('segment', 'span')", name=op.f("ck_training_dataset_item_scope")
        ),
        sa.CheckConstraint(
            "split IN ('train', 'validation', 'test')", name=op.f("ck_training_dataset_item_split")
        ),
        sa.CheckConstraint(
            "training_label IN ('gold', 'silver', 'candidate')",
            name=op.f("ck_training_dataset_item_trainable_label"),
        ),
        sa.ForeignKeyConstraint(
            ["annotation_version_id"],
            ["annotation_version.id"],
            name=op.f("fk_training_dataset_item_annotation_version_id_annotation_version"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["training_dataset.id"],
            name=op.f("fk_training_dataset_item_dataset_id_training_dataset"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["segment.id"], name=op.f("fk_training_dataset_item_segment_id_segment")
        ),
        sa.PrimaryKeyConstraint("dataset_id", "ordinal", name=op.f("pk_training_dataset_item")),
        sa.UniqueConstraint(
            "dataset_id",
            "annotation_version_id",
            name=op.f("uq_training_dataset_item_dataset_id_annotation_version_id"),
        ),
    )
    op.create_index(
        "ix_training_dataset_item_segment", "training_dataset_item", ["segment_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_training_dataset_item_segment", table_name="training_dataset_item")
    op.drop_table("training_dataset_item")
    op.drop_table("training_dataset")
