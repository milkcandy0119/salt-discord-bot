"""建立階段 9 試跑範圍、觀測、每日上限與固定分類評價。

版本 ID：20260804_0007
上一版本：20260804_0006
建立時間：2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0007"
down_revision: str | None = "20260804_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """只建立空白試跑結構；migration 不會自動開始試跑。"""

    op.create_table(
        "trial_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("guild_ids", sa.JSON(), nullable=False),
        sa.Column("channel_ids", sa.JSON(), nullable=False),
        sa.Column("companion_channel_ids", sa.JSON(), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("baseline_global_committed_microusd", sa.Integer(), nullable=False),
        sa.Column("baseline_background_committed_microusd", sa.Integer(), nullable=False),
        sa.Column("global_increment_limit_microusd", sa.Integer(), nullable=False),
        sa.Column("background_increment_limit_microusd", sa.Integer(), nullable=False),
        sa.Column("companion_daily_reply_limit", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_reason", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'stopped')",
            name="ck_trial_session_status",
        ),
        sa.CheckConstraint("ends_at > started_at", name="ck_trial_session_time_range"),
        sa.CheckConstraint(
            "global_increment_limit_microusd > 0",
            name="ck_trial_global_limit_positive",
        ),
        sa.CheckConstraint(
            "background_increment_limit_microusd > 0",
            name="ck_trial_background_limit_positive",
        ),
        sa.CheckConstraint(
            "companion_daily_reply_limit > 0",
            name="ck_trial_companion_limit_positive",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trial_sessions_status", "trial_sessions", ["status"])

    op.create_table(
        "trial_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=True),
        sa.Column("channel_id", sa.String(length=32), nullable=True),
        sa.Column("message_id", sa.String(length=32), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("channel_mode", sa.String(length=16), nullable=True),
        sa.Column("trigger_kind", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.String(length=64), nullable=True),
        sa.Column("outcome", sa.String(length=64), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="ck_trial_latency"),
        sa.ForeignKeyConstraint(["session_id"], ["trial_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    for column in (
        "session_id",
        "guild_id",
        "channel_id",
        "message_id",
        "event_type",
        "channel_mode",
        "trigger_kind",
        "reason",
        "outcome",
        "created_at",
    ):
        op.create_index(f"ix_trial_events_{column}", "trial_events", [column])

    op.create_table(
        "trial_daily_counters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("local_date", sa.String(length=10), nullable=False),
        sa.Column("companion_reply_count", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("companion_reply_count >= 0", name="ck_trial_daily_count"),
        sa.ForeignKeyConstraint(["session_id"], ["trial_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "local_date", name="uq_trial_daily_counter"),
    )
    op.create_index(
        "ix_trial_daily_counters_session_id",
        "trial_daily_counters",
        ["session_id"],
    )

    op.create_table(
        "trial_feedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.String(length=32), nullable=False),
        sa.Column("target_message_id", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "category IN ('good', 'too_formal', 'wrong_memory', 'unwanted_reply', "
            "'missed_reply', 'other')",
            name="ck_trial_feedback_category",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["trial_sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "actor_user_id",
            "target_message_id",
            "category",
            name="uq_trial_feedback_once",
        ),
    )
    for column in (
        "session_id",
        "guild_id",
        "actor_user_id",
        "target_message_id",
        "category",
    ):
        op.create_index(f"ix_trial_feedback_{column}", "trial_feedback", [column])


def downgrade() -> None:
    """移除試跑觀測資料，不影響訊息、預算或個人記憶。"""

    for column in (
        "category",
        "target_message_id",
        "actor_user_id",
        "guild_id",
        "session_id",
    ):
        op.drop_index(f"ix_trial_feedback_{column}", table_name="trial_feedback")
    op.drop_table("trial_feedback")
    op.drop_index(
        "ix_trial_daily_counters_session_id", table_name="trial_daily_counters"
    )
    op.drop_table("trial_daily_counters")
    for column in (
        "created_at",
        "outcome",
        "reason",
        "trigger_kind",
        "channel_mode",
        "event_type",
        "message_id",
        "channel_id",
        "guild_id",
        "session_id",
    ):
        op.drop_index(f"ix_trial_events_{column}", table_name="trial_events")
    op.drop_table("trial_events")
    op.drop_index("ix_trial_sessions_status", table_name="trial_sessions")
    op.drop_table("trial_sessions")
