"""Model adjudication (ADR-022): batches, items, and the model_adjudicated text origin.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = postgresql.JSONB(astext_type=sa.Text())
_OBJ = sa.text("'{}'::jsonb")


def upgrade() -> None:
    op.create_table(
        "adjudication_batch",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("created_by", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("params", _JSON, server_default=_OBJ, nullable=False),
        sa.Column("selection", _JSON, server_default=_OBJ, nullable=False),
        sa.Column("pricing", _JSON, server_default=_OBJ, nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Double(), nullable=False),
        sa.Column("max_cost_usd", sa.Double(), nullable=False),
        sa.Column("spent_usd", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'capped', 'cancelled')",
            name=op.f("ck_adjudication_batch_status"),
        ),
        sa.CheckConstraint("max_cost_usd > 0", name=op.f("ck_adjudication_batch_cost_cap")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_adjudication_batch")),
    )
    op.create_table(
        "adjudication_item",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("segment_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_cost_usd", sa.Double(), nullable=False),
        sa.Column("reserve_usd", sa.Double(), nullable=False),
        sa.Column("request", _JSON, server_default=_OBJ, nullable=False),
        sa.Column("response", _JSON, nullable=True),
        sa.Column("result", _JSON, nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("speech_present", sa.Boolean(), nullable=True),
        sa.Column("confidence", sa.Double(), nullable=True),
        sa.Column("best_hypothesis_similarity", sa.Double(), nullable=True),
        sa.Column("best_hypothesis_model", sa.Text(), nullable=True),
        sa.Column("representative_similarity", sa.Double(), nullable=True),
        sa.Column("usage", _JSON, server_default=_OBJ, nullable=False),
        sa.Column("cost_usd", sa.Double(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("accepted_version_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'skipped', 'cancelled')",
            name=op.f("ck_adjudication_item_status"),
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["adjudication_batch.id"],
            name=op.f("fk_adjudication_item_batch_id_adjudication_batch"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["segment.id"], name=op.f("fk_adjudication_item_segment_id_segment")
        ),
        sa.ForeignKeyConstraint(
            ["accepted_version_id"],
            ["annotation_version.id"],
            name=op.f("fk_adjudication_item_accepted_version_id_annotation_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_adjudication_item")),
        sa.UniqueConstraint(
            "batch_id", "segment_id", name=op.f("uq_adjudication_item_batch_id_segment_id")
        ),
    )
    op.create_index("ix_adjudication_item_status", "adjudication_item", ["status", "batch_id"])
    op.create_index("ix_adjudication_item_segment", "adjudication_item", ["segment_id"])
    op.drop_constraint(
        op.f("ck_annotation_version_text_origin"), "annotation_version", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_annotation_version_text_origin"),
        "annotation_version",
        "text_origin IS NULL OR text_origin IN ('human', 'model_consensus', 'model_adjudicated')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_annotation_version_text_origin"), "annotation_version", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_annotation_version_text_origin"),
        "annotation_version",
        "text_origin IS NULL OR text_origin IN ('human', 'model_consensus')",
    )
    op.drop_index("ix_adjudication_item_segment", table_name="adjudication_item")
    op.drop_index("ix_adjudication_item_status", table_name="adjudication_item")
    op.drop_table("adjudication_item")
    op.drop_table("adjudication_batch")
