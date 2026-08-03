"""不呼叫付費 API 的確定性對話切段引擎。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import ConversationSegmentRecord, MessageRecord


@dataclass(frozen=True, slots=True)
class SegmentAssignment:
    """訊息切段結果。"""

    segment_id: int
    created_segment: bool
    reopened_segment: bool


def _as_utc(value: datetime) -> datetime:
    """補上 SQLite 可能未保留的 UTC 時區資訊。"""

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class ConversationSegmenter:
    """依回覆鏈、活動時間與參與者執行保守切段。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        archive_after: timedelta = timedelta(minutes=30),
        implicit_continuation_window: timedelta = timedelta(minutes=5),
    ) -> None:
        self._session_factory = session_factory
        self._archive_after = archive_after
        self._implicit_continuation_window = implicit_continuation_window

    async def assign_message(self, discord_message_id: str) -> SegmentAssignment:
        """冪等地將訊息指派至既有段落或新的段落。"""

        async with self._session_factory() as session:
            message = await session.scalar(
                select(MessageRecord).where(
                    MessageRecord.discord_message_id == discord_message_id
                )
            )
            if message is None:
                raise LookupError("找不到要切段的 Discord 訊息")

            message_time = _as_utc(message.discord_created_at)
            await self._archive_inactive_in_session(session, message_time)

            if message.segment_id is not None:
                await session.commit()
                return SegmentAssignment(message.segment_id, False, False)

            segment = None
            reopened = False
            if message.replied_to_message_id is not None:
                segment = await self._find_reply_segment(session, message)
            else:
                segment = await self._find_unique_implicit_segment(session, message, message_time)

            created = segment is None
            if segment is None:
                segment = ConversationSegmentRecord(
                    guild_id=message.guild_id,
                    channel_id=message.channel_id,
                    root_message_id=message.discord_message_id,
                    status="active",
                    created_at=message_time,
                    last_message_at=message_time,
                )
                session.add(segment)
                await session.flush()
            else:
                if segment.status == "archived":
                    segment.status = "active"
                    segment.archived_at = None
                    segment.reopened_at = message_time
                    reopened = True
                segment.last_message_at = max(_as_utc(segment.last_message_at), message_time)

            message.segment_id = segment.id
            if not message.is_sensitive:
                message.processing_status = "segmented"
            await session.commit()
            return SegmentAssignment(segment.id, created, reopened)

    async def _find_reply_segment(
        self,
        session: AsyncSession,
        message: MessageRecord,
    ) -> ConversationSegmentRecord | None:
        replied_message = await session.scalar(
            select(MessageRecord).where(
                MessageRecord.discord_message_id == message.replied_to_message_id
            )
        )
        if replied_message is None or replied_message.segment_id is None:
            return None
        segment = await session.get(ConversationSegmentRecord, replied_message.segment_id)
        if (
            segment is None
            or segment.guild_id != message.guild_id
            or segment.channel_id != message.channel_id
        ):
            return None
        return segment

    async def _find_unique_implicit_segment(
        self,
        session: AsyncSession,
        message: MessageRecord,
        message_time: datetime,
    ) -> ConversationSegmentRecord | None:
        cutoff = message_time - self._implicit_continuation_window
        candidate_ids = (
            await session.scalars(
                select(ConversationSegmentRecord.id)
                .join(MessageRecord, MessageRecord.segment_id == ConversationSegmentRecord.id)
                .where(
                    ConversationSegmentRecord.guild_id == message.guild_id,
                    ConversationSegmentRecord.channel_id == message.channel_id,
                    ConversationSegmentRecord.status == "active",
                    ConversationSegmentRecord.last_message_at >= cutoff,
                    MessageRecord.author_id == message.author_id,
                    MessageRecord.discord_created_at >= cutoff,
                )
                .distinct()
            )
        ).all()
        if len(candidate_ids) != 1:
            return None
        return await session.get(ConversationSegmentRecord, candidate_ids[0])

    async def archive_inactive(self, as_of: datetime | None = None) -> int:
        """封存滿 30 分鐘沒有新訊息的活動段落。"""

        return len(await self.archive_inactive_segment_ids(as_of))

    async def archive_inactive_segment_ids(
        self,
        as_of: datetime | None = None,
    ) -> tuple[int, ...]:
        """封存逾時段落並傳回這次實際封存的 ID。"""

        effective_time = _as_utc(as_of or datetime.now(UTC))
        async with self._session_factory() as session:
            archived = await self._archive_inactive_in_session(session, effective_time)
            await session.commit()
            return archived

    async def _archive_inactive_in_session(
        self,
        session: AsyncSession,
        as_of: datetime,
    ) -> tuple[int, ...]:
        cutoff = as_of - self._archive_after
        return tuple(
            (
                await session.scalars(
                    update(ConversationSegmentRecord)
                    .where(
                        ConversationSegmentRecord.status == "active",
                        ConversationSegmentRecord.last_message_at <= cutoff,
                    )
                    .values(status="archived", archived_at=as_of)
                    .returning(ConversationSegmentRecord.id)
                )
            ).all()
        )

    async def assign_pending_messages(self) -> int:
        """在啟動時補處理已保存但尚未切段的訊息。"""

        async with self._session_factory() as session:
            message_ids = (
                await session.scalars(
                    select(MessageRecord.discord_message_id)
                    .where(MessageRecord.segment_id.is_(None))
                    .order_by(MessageRecord.discord_created_at, MessageRecord.id)
                )
            ).all()
        for message_id in message_ids:
            await self.assign_message(message_id)
        return len(message_ids)

    async def get_segment_state(self, segment_id: int) -> str | None:
        """查詢段落目前是活動或封存狀態。"""

        async with self._session_factory() as session:
            return await session.scalar(
                select(ConversationSegmentRecord.status).where(
                    ConversationSegmentRecord.id == segment_id
                )
            )

    async def count_segments(self) -> int:
        """傳回對話段落總數。"""

        async with self._session_factory() as session:
            return int(
                await session.scalar(
                    select(func.count()).select_from(ConversationSegmentRecord)
                )
                or 0
            )
