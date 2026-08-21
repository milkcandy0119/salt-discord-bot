"""Persist the operating mode for each allowlisted channel.

Revision ID: 20260813_0011
Revises: 20260808_0010
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0011"
down_revision: str | None = "20260808_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("channel_allowlists") as batch_op:
        batch_op.add_column(
            sa.Column("mode", sa.String(length=16), nullable=False, server_default="normal")
        )
        batch_op.create_check_constraint(
            "ck_channel_allowlist_mode",
            "mode IN ('normal', 'companion')",
        )


def downgrade() -> None:
    with op.batch_alter_table("channel_allowlists") as batch_op:
        batch_op.drop_constraint("ck_channel_allowlist_mode", type_="check")
        batch_op.drop_column("mode")
