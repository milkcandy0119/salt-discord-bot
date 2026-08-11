"""Add recurrence metadata to persisted reminders.

Revision ID: 20260808_0010
Revises: 20260805_0009
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260808_0010"
down_revision: str | None = "20260805_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reminders") as batch_op:
        batch_op.add_column(
            sa.Column(
                "recurrence_kind",
                sa.String(length=16),
                nullable=False,
                server_default="once",
            )
        )
        batch_op.add_column(sa.Column("recurrence_time", sa.String(length=5), nullable=True))
        batch_op.add_column(sa.Column("recurrence_weekdays", sa.String(length=13), nullable=True))
        batch_op.add_column(sa.Column("interval_days", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("recurrence_start_date", sa.String(length=10), nullable=True))
        batch_op.create_check_constraint(
            "ck_reminder_recurrence_kind",
            "recurrence_kind IN ('once', 'daily', 'weekly', 'interval')",
        )


def downgrade() -> None:
    with op.batch_alter_table("reminders") as batch_op:
        batch_op.drop_constraint("ck_reminder_recurrence_kind", type_="check")
        batch_op.drop_column("recurrence_start_date")
        batch_op.drop_column("interval_days")
        batch_op.drop_column("recurrence_weekdays")
        batch_op.drop_column("recurrence_time")
        batch_op.drop_column("recurrence_kind")
