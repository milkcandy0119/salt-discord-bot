"""建立持久化提醒、使用者時區與管理稽核。

版本 ID：20260804_0006
上一版本：20260804_0005
建立時間：2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0006"
down_revision: str | None = "20260804_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立免費提醒與管理資料結構，不建立任何預設提醒。"""

    op.create_table(
        "user_timezones",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id", "user_id", name="uq_user_timezone_owner"),
    )
    op.create_index("ix_user_timezones_guild_id", "user_timezones", ["guild_id"])
    op.create_index("ix_user_timezones_user_id", "user_timezones", ["user_id"])

    op.create_table(
        "reminders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("timezone_name", sa.String(length=64), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(content) BETWEEN 1 AND 500", name="ck_reminder_content_length"),
        sa.CheckConstraint("attempts >= 0", name="ck_reminder_attempts"),
        sa.CheckConstraint("max_attempts > 0", name="ck_reminder_max_attempts"),
        sa.CheckConstraint(
            "status IN ('pending', 'sending', 'retry_wait', 'sent', 'cancelled', 'failed')",
            name="ck_reminder_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reminders_guild_id", "reminders", ["guild_id"])
    op.create_index("ix_reminders_user_id", "reminders", ["user_id"])
    op.create_index("ix_reminders_due_at", "reminders", ["due_at"])
    op.create_index("ix_reminders_status", "reminders", ["status"])
    op.create_index("ix_reminders_available_at", "reminders", ["available_at"])
    op.create_index(
        "ix_reminders_ready_order",
        "reminders",
        ["status", "due_at", "available_at", "id"],
    )

    op.create_table(
        "admin_audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("target_user_id", sa.String(length=32), nullable=True),
        sa.Column("target_record_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admin_audit_events_guild_id", "admin_audit_events", ["guild_id"])
    op.create_index(
        "ix_admin_audit_events_actor_user_id",
        "admin_audit_events",
        ["actor_user_id"],
    )
    op.create_index("ix_admin_audit_events_action", "admin_audit_events", ["action"])
    op.create_index(
        "ix_admin_audit_events_target_user_id",
        "admin_audit_events",
        ["target_user_id"],
    )


def downgrade() -> None:
    """移除提醒、時區與管理稽核資料。"""

    op.drop_index("ix_admin_audit_events_target_user_id", table_name="admin_audit_events")
    op.drop_index("ix_admin_audit_events_action", table_name="admin_audit_events")
    op.drop_index("ix_admin_audit_events_actor_user_id", table_name="admin_audit_events")
    op.drop_index("ix_admin_audit_events_guild_id", table_name="admin_audit_events")
    op.drop_table("admin_audit_events")
    op.drop_index("ix_reminders_ready_order", table_name="reminders")
    op.drop_index("ix_reminders_available_at", table_name="reminders")
    op.drop_index("ix_reminders_status", table_name="reminders")
    op.drop_index("ix_reminders_due_at", table_name="reminders")
    op.drop_index("ix_reminders_user_id", table_name="reminders")
    op.drop_index("ix_reminders_guild_id", table_name="reminders")
    op.drop_table("reminders")
    op.drop_index("ix_user_timezones_user_id", table_name="user_timezones")
    op.drop_index("ix_user_timezones_guild_id", table_name="user_timezones")
    op.drop_table("user_timezones")
