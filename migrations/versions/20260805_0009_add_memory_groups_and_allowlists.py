"""加入持久化頻道白名單與記憶分組。

版本 ID：20260805_0009
上一版本：20260804_0008
建立時間：2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260805_0009"
down_revision: str | None = "20260804_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_allowlists",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id", "channel_id", name="uq_channel_allowlist"),
    )
    op.create_index("ix_channel_allowlists_guild_id", "channel_allowlists", ["guild_id"])
    op.create_index("ix_channel_allowlists_channel_id", "channel_allowlists", ["channel_id"])
    op.create_table(
        "memory_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id", "name", name="uq_memory_group_guild_name"),
    )
    op.create_index("ix_memory_groups_guild_id", "memory_groups", ["guild_id"])
    op.create_table(
        "memory_group_channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("group_id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["memory_groups.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id", "channel_id", name="uq_memory_group_channel"),
        sa.UniqueConstraint("group_id", "channel_id", name="uq_memory_group_member"),
    )
    op.create_index("ix_memory_group_channels_group_id", "memory_group_channels", ["group_id"])
    op.create_index("ix_memory_group_channels_guild_id", "memory_group_channels", ["guild_id"])
    op.create_index("ix_memory_group_channels_channel_id", "memory_group_channels", ["channel_id"])
    op.create_table(
        "pending_actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("action_type", sa.String(length=32), nullable=False),
        sa.Column("parsed_parameters", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'executing', 'completed', 'failed')",
            name="ck_pending_action_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("guild_id", "channel_id", "user_id", "action_type", "expires_at", "status"):
        op.create_index(f"ix_pending_actions_{column}", "pending_actions", [column])


def downgrade() -> None:
    for column in ("status", "expires_at", "action_type", "user_id", "channel_id", "guild_id"):
        op.drop_index(f"ix_pending_actions_{column}", table_name="pending_actions")
    op.drop_table("pending_actions")
    op.drop_index("ix_memory_group_channels_channel_id", table_name="memory_group_channels")
    op.drop_index("ix_memory_group_channels_guild_id", table_name="memory_group_channels")
    op.drop_index("ix_memory_group_channels_group_id", table_name="memory_group_channels")
    op.drop_table("memory_group_channels")
    op.drop_index("ix_memory_groups_guild_id", table_name="memory_groups")
    op.drop_table("memory_groups")
    op.drop_index("ix_channel_allowlists_channel_id", table_name="channel_allowlists")
    op.drop_index("ix_channel_allowlists_guild_id", table_name="channel_allowlists")
    op.drop_table("channel_allowlists")
