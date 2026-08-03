"""建立以 Discord user ID 隔離的基本個人記憶。

版本 ID：20260804_0005
上一版本：20260804_0004
建立時間：2026-08-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260804_0005"
down_revision: str | None = "20260804_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立個人記憶表，不從既有聊天自動推測或回填資料。"""

    op.create_table(
        "personal_memories",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("guild_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_content", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("source_message_id", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(content) BETWEEN 1 AND 200", name="ck_memory_content_length"),
        sa.CheckConstraint(
            "source_type IN ('chat', 'slash')",
            name="ck_memory_source_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_message_id"),
        sa.UniqueConstraint(
            "guild_id",
            "user_id",
            "normalized_content",
            name="uq_personal_memory_owner_content",
        ),
    )
    op.create_index("ix_personal_memories_guild_id", "personal_memories", ["guild_id"])
    op.create_index("ix_personal_memories_user_id", "personal_memories", ["user_id"])
    op.create_index(
        "ix_personal_memories_source_message_id",
        "personal_memories",
        ["source_message_id"],
        unique=True,
    )
    op.create_index(
        "ix_personal_memories_owner_updated",
        "personal_memories",
        ["guild_id", "user_id", "updated_at", "id"],
    )


def downgrade() -> None:
    """移除基本個人記憶。"""

    op.drop_index("ix_personal_memories_owner_updated", table_name="personal_memories")
    op.drop_index(
        "ix_personal_memories_source_message_id",
        table_name="personal_memories",
    )
    op.drop_index("ix_personal_memories_user_id", table_name="personal_memories")
    op.drop_index("ix_personal_memories_guild_id", table_name="personal_memories")
    op.drop_table("personal_memories")
