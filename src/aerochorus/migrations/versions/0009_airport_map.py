"""Airport map context: runway-end coordinates and FAA controlled-airspace polygons.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "airport_airspace",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.Column("icao", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("airspace_class", sa.Text(), nullable=False),
        sa.Column("local_type", sa.Text(), nullable=True),
        sa.Column("lower_ft", sa.Integer(), nullable=True),
        sa.Column("lower_ref", sa.Text(), nullable=True),
        sa.Column("upper_ft", sa.Integer(), nullable=True),
        sa.Column("upper_ref", sa.Text(), nullable=True),
        sa.Column("rings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column(
            "provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["icao"],
            ["airport.icao"],
            name=op.f("fk_airport_airspace_icao_airport"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_airport_airspace")),
    )
    op.create_index("ix_airport_airspace_icao", "airport_airspace", ["icao"], unique=False)
    op.add_column("airport_runway", sa.Column("latitude", sa.Double(), nullable=True))
    op.add_column("airport_runway", sa.Column("longitude", sa.Double(), nullable=True))
    op.add_column("airport_runway", sa.Column("elevation_ft", sa.Double(), nullable=True))
    op.add_column("airport_runway", sa.Column("displaced_latitude", sa.Double(), nullable=True))
    op.add_column("airport_runway", sa.Column("displaced_longitude", sa.Double(), nullable=True))


def downgrade() -> None:
    op.drop_column("airport_runway", "displaced_longitude")
    op.drop_column("airport_runway", "displaced_latitude")
    op.drop_column("airport_runway", "elevation_ft")
    op.drop_column("airport_runway", "longitude")
    op.drop_column("airport_runway", "latitude")
    op.drop_index("ix_airport_airspace_icao", table_name="airport_airspace")
    op.drop_table("airport_airspace")
