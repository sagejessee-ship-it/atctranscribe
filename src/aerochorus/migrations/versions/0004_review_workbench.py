"""Review workbench: agreement, versioned annotations, samples, airports, search.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Trigram indexes power transcript search (no separate search service).
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "airport",
        sa.Column("icao", sa.Text(), nullable=False),
        sa.Column("faa_id", sa.Text(), nullable=True),
        sa.Column("iata", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("latitude", sa.Double(), nullable=True),
        sa.Column("longitude", sa.Double(), nullable=True),
        sa.Column("elevation_ft", sa.Double(), nullable=True),
        sa.Column("magnetic_variation", sa.Text(), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("icao ~ '^[A-Z0-9]{4}$'", name=op.f("ck_airport_icao_format")),
        sa.PrimaryKeyConstraint("icao", name=op.f("pk_airport")),
    )
    op.create_table(
        "review_sample",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("filters", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("filter_sha256", sa.Text(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("total_matching", sa.Integer(), nullable=False),
        sa.Column("segment_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_sample")),
    )
    op.create_table(
        "airport_alias",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("icao", sa.Text(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('icao', 'faa', 'iata', 'station', 'name', 'spoken')",
            name=op.f("ck_airport_alias_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["icao"],
            ["airport.icao"],
            name=op.f("fk_airport_alias_icao_airport"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_airport_alias")),
        sa.UniqueConstraint("icao", "alias", "kind", name=op.f("uq_airport_alias_icao_alias_kind")),
    )
    op.create_index("ix_airport_alias_lookup", "airport_alias", ["kind", "alias"], unique=False)
    op.create_table(
        "airport_frequency",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("icao", sa.Text(), nullable=False),
        sa.Column("service", sa.Text(), nullable=False),
        sa.Column("frequency_hz", sa.BigInteger(), nullable=False),
        sa.Column("facility", sa.Text(), nullable=True),
        sa.Column("call", sa.Text(), nullable=True),
        sa.Column("sectorization", sa.Text(), nullable=True),
        sa.Column(
            "spoken",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["icao"],
            ["airport.icao"],
            name=op.f("fk_airport_frequency_icao_airport"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_airport_frequency")),
    )
    op.create_index(
        "ix_airport_frequency_hz", "airport_frequency", ["icao", "frequency_hz"], unique=False
    )
    op.create_table(
        "airport_runway",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("icao", sa.Text(), nullable=False),
        sa.Column("pair", sa.Text(), nullable=False),
        sa.Column("end_ident", sa.Text(), nullable=False),
        sa.Column("length_ft", sa.Integer(), nullable=True),
        sa.Column("width_ft", sa.Integer(), nullable=True),
        sa.Column("true_alignment", sa.Double(), nullable=True),
        sa.Column(
            "spoken",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["icao"],
            ["airport.icao"],
            name=op.f("fk_airport_runway_icao_airport"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_airport_runway")),
        sa.UniqueConstraint("icao", "end_ident", name=op.f("uq_airport_runway_icao_end_ident")),
    )
    op.create_table(
        "annotation_thread",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("segment_id", sa.BigInteger(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("current_version_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("scope IN ('segment', 'span')", name=op.f("ck_annotation_thread_scope")),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["segment.id"], name=op.f("fk_annotation_thread_segment_id_segment")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_annotation_thread")),
    )
    op.create_index(
        "ix_annotation_thread_segment", "annotation_thread", ["segment_id"], unique=False
    )
    op.create_index(
        "uq_annotation_thread_one_segment_scope",
        "annotation_thread",
        ["segment_id"],
        unique=True,
        postgresql_where=sa.text("scope = 'segment'"),
    )
    op.create_table(
        "segment_agreement",
        sa.Column("segment_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("near_threshold", sa.Double(), nullable=False),
        sa.Column("results_count", sa.Integer(), nullable=False),
        sa.Column("success_count", sa.Integer(), nullable=False),
        sa.Column("abstained_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column(
            "models",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "families",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("best_exact_provider_count", sa.Integer(), nullable=False),
        sa.Column("best_exact_family_count", sa.Integer(), nullable=False),
        sa.Column("best_near_family_count", sa.Integer(), nullable=False),
        sa.Column("best_near_similarity", sa.Double(), nullable=True),
        sa.Column("max_pair_similarity", sa.Double(), nullable=True),
        sa.Column("representative_text", sa.Text(), nullable=True),
        sa.Column("representative_source", sa.Text(), nullable=True),
        sa.Column(
            "exact_groups",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("near_group", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "flags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["segment_id"], ["segment.id"], name=op.f("fk_segment_agreement_segment_id_segment")
        ),
        sa.PrimaryKeyConstraint("segment_id", name=op.f("pk_segment_agreement")),
    )
    op.create_index(
        "ix_segment_agreement_exact_families",
        "segment_agreement",
        ["best_exact_family_count"],
        unique=False,
    )
    op.create_index(
        "ix_segment_agreement_exact_providers",
        "segment_agreement",
        ["best_exact_provider_count"],
        unique=False,
    )
    op.create_index(
        "ix_segment_agreement_flags",
        "segment_agreement",
        ["flags"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_segment_agreement_near_families",
        "segment_agreement",
        ["best_near_family_count"],
        unique=False,
    )
    op.create_index(
        "ix_segment_agreement_representative_trgm",
        "segment_agreement",
        ["representative_text"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"representative_text": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_segment_agreement_results", "segment_agreement", ["results_count"], unique=False
    )
    op.create_table(
        "annotation_version",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("start_ms", sa.Integer(), nullable=True),
        sa.Column("end_ms", sa.Integer(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("text_origin", sa.Text(), nullable=True),
        sa.Column("review_status", sa.Text(), nullable=False),
        sa.Column("training_label", sa.Text(), nullable=False),
        sa.Column(
            "reason_tags",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("annotator", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column(
            "basis",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "review_status IN ('unreviewed', 'reviewed', 'corrected')",
            name=op.f("ck_annotation_version_review_status"),
        ),
        sa.CheckConstraint(
            "text_origin IS NULL OR text_origin IN ('human', 'model_consensus')",
            name=op.f("ck_annotation_version_text_origin"),
        ),
        sa.CheckConstraint(
            "training_label <> 'gold' OR (text_origin = 'human' AND coalesce(btrim(text), '') <> '' AND review_status IN ('reviewed', 'corrected'))",
            name=op.f("ck_annotation_version_gold_is_human"),
        ),
        sa.CheckConstraint(
            "training_label IN ('none', 'candidate', 'silver', 'gold', 'rejected')",
            name=op.f("ck_annotation_version_training_label"),
        ),
        sa.CheckConstraint(
            "(start_ms IS NULL) = (end_ms IS NULL)",
            name=op.f("ck_annotation_version_span_bounds_paired"),
        ),
        sa.CheckConstraint(
            "end_ms IS NULL OR end_ms > start_ms",
            name=op.f("ck_annotation_version_span_bounds_order"),
        ),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["annotation_thread.id"],
            name=op.f("fk_annotation_version_thread_id_annotation_thread"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_annotation_version")),
        sa.UniqueConstraint(
            "thread_id", "version", name=op.f("uq_annotation_version_thread_id_version")
        ),
    )
    op.create_index(
        "ix_annotation_version_text_trgm",
        "annotation_version",
        ["text"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"text": "gin_trgm_ops"},
    )
    op.add_column(
        "corpus_source",
        sa.Column("role", sa.Text(), server_default=sa.text("'corpus'"), nullable=False),
    )
    op.add_column(
        "model",
        sa.Column(
            "qualification",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    # annotation_thread <-> annotation_version reference each other.
    op.create_foreign_key(
        op.f("fk_annotation_thread_current_version_id_annotation_version"),
        "annotation_thread",
        "annotation_version",
        ["current_version_id"],
        ["id"],
    )
    # Hypothesis text search, and same-channel neighbor lookups.
    op.create_index(
        "ix_transcription_result_text_trgm",
        "transcription_result",
        ["text"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"text": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_segment_source_channel_utc",
        "segment",
        ["source_id", "channel", "capture_start_utc"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_segment_source_channel_utc", table_name="segment")
    op.drop_index(
        "ix_transcription_result_text_trgm",
        table_name="transcription_result",
        postgresql_using="gin",
        postgresql_ops={"text": "gin_trgm_ops"},
    )
    op.drop_constraint(
        op.f("fk_annotation_thread_current_version_id_annotation_version"),
        "annotation_thread",
        type_="foreignkey",
    )
    op.drop_column("model", "qualification")
    op.drop_column("corpus_source", "role")
    op.drop_index(
        "ix_annotation_version_text_trgm",
        table_name="annotation_version",
        postgresql_using="gin",
        postgresql_ops={"text": "gin_trgm_ops"},
    )
    op.drop_table("annotation_version")
    op.drop_index("ix_segment_agreement_results", table_name="segment_agreement")
    op.drop_index(
        "ix_segment_agreement_representative_trgm",
        table_name="segment_agreement",
        postgresql_using="gin",
        postgresql_ops={"representative_text": "gin_trgm_ops"},
    )
    op.drop_index("ix_segment_agreement_near_families", table_name="segment_agreement")
    op.drop_index(
        "ix_segment_agreement_flags", table_name="segment_agreement", postgresql_using="gin"
    )
    op.drop_index("ix_segment_agreement_exact_providers", table_name="segment_agreement")
    op.drop_index("ix_segment_agreement_exact_families", table_name="segment_agreement")
    op.drop_table("segment_agreement")
    op.drop_index(
        "uq_annotation_thread_one_segment_scope",
        table_name="annotation_thread",
        postgresql_where=sa.text("scope = 'segment'"),
    )
    op.drop_index("ix_annotation_thread_segment", table_name="annotation_thread")
    op.drop_table("annotation_thread")
    op.drop_table("airport_runway")
    op.drop_index("ix_airport_frequency_hz", table_name="airport_frequency")
    op.drop_table("airport_frequency")
    op.drop_index("ix_airport_alias_lookup", table_name="airport_alias")
    op.drop_table("airport_alias")
    op.drop_table("review_sample")
    op.drop_table("airport")
