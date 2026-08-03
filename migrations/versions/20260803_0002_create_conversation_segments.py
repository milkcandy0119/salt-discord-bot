"""建立對話段落及訊息成員關係。

版本 ID：20260803_0002
上一版本：20260803_0001
建立時間：2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260803_0002"
down_revision: str | None = "20260803_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立活動／封存段落，並讓訊息可指向所屬段落。"""

    op.create_table(
        "conversation_segments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("channel_id", sa.String(length=32), nullable=False),
        sa.Column("root_message_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("root_message_id", name="uq_segments_root_message_id"),
    )
    op.create_index(
        "ix_conversation_segments_guild_id",
        "conversation_segments",
        ["guild_id"],
    )
    op.create_index(
        "ix_conversation_segments_channel_id",
        "conversation_segments",
        ["channel_id"],
    )
    op.create_index(
        "ix_conversation_segments_status",
        "conversation_segments",
        ["status"],
    )
    with op.batch_alter_table("messages") as batch_op:
        batch_op.add_column(sa.Column("segment_id", sa.Integer(), nullable=True))
        batch_op.create_index("ix_messages_segment_id", ["segment_id"])
        batch_op.create_foreign_key(
            "fk_messages_segment_id_conversation_segments",
            "conversation_segments",
            ["segment_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """移除訊息的段落成員關係及對話段落資料表。"""

    with op.batch_alter_table("messages") as batch_op:
        batch_op.drop_constraint(
            "fk_messages_segment_id_conversation_segments",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_messages_segment_id")
        batch_op.drop_column("segment_id")
    op.drop_index("ix_conversation_segments_status", table_name="conversation_segments")
    op.drop_index("ix_conversation_segments_channel_id", table_name="conversation_segments")
    op.drop_index("ix_conversation_segments_guild_id", table_name="conversation_segments")
    op.drop_table("conversation_segments")

