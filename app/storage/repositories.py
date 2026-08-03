"""訊息持久化 repository。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import MessageRecord


@dataclass(frozen=True, slots=True)
class NewMessage:
    """準備寫入資料庫且已通過敏感資料閘門的訊息。"""

    discord_message_id: str
    guild_id: str
    channel_id: str
    author_id: str
    author_display_name: str | None
    content: str
    discord_created_at: datetime
    received_at: datetime
    replied_to_message_id: str | None
    is_bot: bool
    is_sensitive: bool
    sensitive_categories: tuple[str, ...]
    notifications_required: bool = True


@dataclass(frozen=True, slots=True)
class SaveResult:
    """冪等寫入的結果。"""

    message: MessageRecord
    created: bool


class MessageRepository:
    """在獨立交易中保存及查詢 Discord 訊息。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def save(self, message: NewMessage) -> SaveResult:
        """依 Discord message ID 冪等寫入訊息。"""

        notification_status = (
            "pending"
            if message.is_sensitive and message.notifications_required
            else "not_required"
        )
        values = {
            "discord_message_id": message.discord_message_id,
            "guild_id": message.guild_id,
            "channel_id": message.channel_id,
            "author_id": message.author_id,
            "author_display_name": message.author_display_name,
            "content": message.content,
            "discord_created_at": message.discord_created_at,
            "received_at": message.received_at,
            "replied_to_message_id": message.replied_to_message_id,
            "is_bot": message.is_bot,
            "is_sensitive": message.is_sensitive,
            "sensitive_categories": list(message.sensitive_categories),
            "processing_status": (
                "blocked_sensitive" if message.is_sensitive else "pending_segmentation"
            ),
            "author_notification_status": notification_status,
            "admin_notification_status": notification_status,
        }

        async with self._session_factory() as session:
            statement = (
                sqlite_insert(MessageRecord)
                .values(**values)
                .on_conflict_do_nothing(index_elements=["discord_message_id"])
            )
            result = await session.execute(statement)
            await session.commit()
            stored = await session.scalar(
                select(MessageRecord).where(
                    MessageRecord.discord_message_id == message.discord_message_id
                )
            )

        if stored is None:
            raise RuntimeError("冪等寫入後找不到訊息紀錄")
        return SaveResult(message=stored, created=result.rowcount == 1)

    async def get_by_discord_id(self, discord_message_id: str) -> MessageRecord | None:
        """以 Discord message ID 查詢訊息。"""

        async with self._session_factory() as session:
            return await session.scalar(
                select(MessageRecord).where(
                    MessageRecord.discord_message_id == discord_message_id
                )
            )

    async def count(self) -> int:
        """傳回已保存訊息總數。"""

        async with self._session_factory() as session:
            return int(await session.scalar(select(func.count()).select_from(MessageRecord)) or 0)

    async def list_recent_in_channel(
        self,
        channel_id: str,
        *,
        since: datetime,
        limit: int = 200,
    ) -> tuple[MessageRecord, ...]:
        """依時間讀取頻道近期訊息，供免費陪伴判斷使用。"""

        if limit <= 0:
            raise ValueError("近期訊息查詢上限必須大於零")
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(MessageRecord)
                    .where(
                        MessageRecord.channel_id == channel_id,
                        MessageRecord.discord_created_at >= since,
                    )
                    .order_by(MessageRecord.discord_created_at.desc(), MessageRecord.id.desc())
                    .limit(limit)
                )
            ).all()
            return tuple(rows)

    async def update_notification_statuses(
        self,
        discord_message_id: str,
        *,
        author_status: str,
        admin_status: str,
    ) -> None:
        """保存敏感事件通知結果。"""

        async with self._session_factory() as session:
            await session.execute(
                update(MessageRecord)
                .where(MessageRecord.discord_message_id == discord_message_id)
                .values(
                    author_notification_status=author_status,
                    admin_notification_status=admin_status,
                )
            )
            await session.commit()
