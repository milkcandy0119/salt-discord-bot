"""建立 Discord 訊息資料表。

版本 ID：20260803_0001
上一版本：無
建立時間：2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立訊息、回覆關係、敏感狀態與通知狀態欄位。"""

    op.create_table(
        "messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("discord_message_id", sa.String(length=32), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("author_id", sa.String(length=32), nullable=False),
        sa.Column("author_display_name", sa.String(length=128), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("discord_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("replied_to_message_id", sa.String(length=32), nullable=True),
        sa.Column("is_bot", sa.Boolean(), nullable=False),
        sa.Column("is_sensitive", sa.Boolean(), nullable=False),
        sa.Column("sensitive_categories", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("processing_status", sa.String(length=32), nullable=False),
        sa.Column("author_notification_status", sa.String(length=32), nullable=False),
        sa.Column("admin_notification_status", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("discord_message_id", name="uq_messages_discord_message_id"),
    )
    op.create_index("ix_messages_guild_id", "messages", ["guild_id"])
    op.create_index("ix_messages_channel_id", "messages", ["channel_id"])
    op.create_index("ix_messages_author_id", "messages", ["author_id"])
    op.create_index(
        "ix_messages_replied_to_message_id", "messages", ["replied_to_message_id"]
    )
    op.create_index("ix_messages_is_sensitive", "messages", ["is_sensitive"])


def downgrade() -> None:
    """移除階段 1 的訊息資料表。"""

    op.drop_index("ix_messages_is_sensitive", table_name="messages")
    op.drop_index("ix_messages_replied_to_message_id", table_name="messages")
    op.drop_index("ix_messages_author_id", table_name="messages")
    op.drop_index("ix_messages_channel_id", table_name="messages")
    op.drop_index("ix_messages_guild_id", table_name="messages")
    op.drop_table("messages")
