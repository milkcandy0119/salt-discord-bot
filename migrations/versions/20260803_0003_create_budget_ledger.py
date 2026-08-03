"""建立一次性預算帳本與門檻通知狀態。

版本 ID：20260803_0003
上一版本：20260803_0002
建立時間：2026-08-03
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0003"
down_revision: str | None = "20260803_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立精確微美元預算狀態、付費呼叫及通知資料表。"""

    budget_state = op.create_table(
        "budget_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("global_spent_microusd", sa.Integer(), nullable=False),
        sa.Column("global_reserved_microusd", sa.Integer(), nullable=False),
        sa.Column("background_spent_microusd", sa.Integer(), nullable=False),
        sa.Column("background_reserved_microusd", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("global_spent_microusd >= 0", name="ck_budget_global_spent"),
        sa.CheckConstraint("global_reserved_microusd >= 0", name="ck_budget_global_reserved"),
        sa.CheckConstraint("background_spent_microusd >= 0", name="ck_budget_background_spent"),
        sa.CheckConstraint(
            "background_reserved_microusd >= 0",
            name="ck_budget_background_reserved",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        budget_state,
        [
            {
                "id": 1,
                "global_spent_microusd": 0,
                "global_reserved_microusd": 0,
                "background_spent_microusd": 0,
                "background_reserved_microusd": 0,
                "updated_at": datetime.now(UTC),
            }
        ],
    )

    op.create_table(
        "paid_ai_calls",
        sa.Column("reservation_id", sa.String(length=36), nullable=False),
        sa.Column("purpose", sa.String(length=32), nullable=False),
        sa.Column("budget_scope", sa.String(length=16), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("price_version", sa.String(length=64), nullable=False),
        sa.Column("input_microusd_per_million_tokens", sa.Integer(), nullable=False),
        sa.Column("output_microusd_per_million_tokens", sa.Integer(), nullable=False),
        sa.Column("maximum_input_tokens", sa.Integer(), nullable=False),
        sa.Column("maximum_output_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_cost_microusd", sa.Integer(), nullable=False),
        sa.Column("actual_cost_microusd", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("reserved_cost_microusd >= 0", name="ck_calls_reserved_cost"),
        sa.CheckConstraint("actual_cost_microusd >= 0", name="ck_calls_actual_cost"),
        sa.CheckConstraint("maximum_input_tokens >= 0", name="ck_calls_max_input_tokens"),
        sa.CheckConstraint("maximum_output_tokens >= 0", name="ck_calls_max_output_tokens"),
        sa.PrimaryKeyConstraint("reservation_id"),
    )
    op.create_index("ix_paid_ai_calls_purpose", "paid_ai_calls", ["purpose"])
    op.create_index("ix_paid_ai_calls_status", "paid_ai_calls", ["status"])
    op.create_index("ix_paid_ai_calls_created_at", "paid_ai_calls", ["created_at"])

    op.create_table(
        "budget_threshold_notifications",
        sa.Column("threshold_percent", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_type", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "threshold_percent IN (70, 90)",
            name="ck_budget_notification_threshold",
        ),
        sa.PrimaryKeyConstraint("threshold_percent"),
    )
    op.create_index(
        "ix_budget_threshold_notifications_status",
        "budget_threshold_notifications",
        ["status"],
    )


def downgrade() -> None:
    """移除預算帳本及門檻通知資料表。"""

    op.drop_index(
        "ix_budget_threshold_notifications_status",
        table_name="budget_threshold_notifications",
    )
    op.drop_table("budget_threshold_notifications")
    op.drop_index("ix_paid_ai_calls_created_at", table_name="paid_ai_calls")
    op.drop_index("ix_paid_ai_calls_status", table_name="paid_ai_calls")
    op.drop_index("ix_paid_ai_calls_purpose", table_name="paid_ai_calls")
    op.drop_table("paid_ai_calls")
    op.drop_table("budget_state")

