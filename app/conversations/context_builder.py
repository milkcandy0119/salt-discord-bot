"""以回覆鏈與目前段落近期訊息建立有上限的聊天上下文。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import MessageRecord


@dataclass(frozen=True, slots=True)
class ProviderInputMessage:
    """可直接轉換成文字生成介面輸入的單一訊息。"""

    role: Literal["user", "assistant"]
    content: str
    discord_message_id: str


@dataclass(frozen=True, slots=True)
class ChatContext:
    """已依優先順序及字元上限縮減的聊天上下文。"""

    trigger_message_id: str
    messages: tuple[ProviderInputMessage, ...]
    character_count: int


class ContextBuilder:
    """優先保留明確回覆鏈，再補入目前段落的近期內容。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        maximum_characters: int,
    ) -> None:
        if maximum_characters <= 0:
            raise ValueError("最大上下文字元數必須大於零")
        self._session_factory = session_factory
        self._maximum_characters = maximum_characters

    async def build(
        self,
        trigger_message_id: str,
        *,
        assistant_author_id: str,
    ) -> ChatContext:
        """建立不包含敏感訊息的段落上下文。"""

        async with self._session_factory() as session:
            trigger = await session.scalar(
                select(MessageRecord).where(
                    MessageRecord.discord_message_id == trigger_message_id
                )
            )
            if trigger is None:
                raise LookupError("找不到觸發 AI 回覆的訊息")
            if trigger.is_sensitive:
                raise ValueError("敏感訊息不得建立外部 AI 上下文")
            if trigger.segment_id is None:
                raise RuntimeError("觸發訊息尚未完成對話切段")
            records = (
                await session.scalars(
                    select(MessageRecord)
                    .where(
                        MessageRecord.segment_id == trigger.segment_id,
                        MessageRecord.is_sensitive.is_(False),
                    )
                    .order_by(MessageRecord.discord_created_at, MessageRecord.id)
                )
            ).all()

        by_message_id = {record.discord_message_id: record for record in records}
        priority = self._priority_order(trigger, records, by_message_id)
        selected: dict[str, ProviderInputMessage] = {}
        remaining = self._maximum_characters
        for record in priority:
            rendered = self._render(record)
            if not rendered or remaining <= 0:
                continue
            clipped = rendered[:remaining]
            selected[record.discord_message_id] = ProviderInputMessage(
                role=(
                    "assistant"
                    if record.author_id == assistant_author_id
                    else "user"
                ),
                content=clipped,
                discord_message_id=record.discord_message_id,
            )
            remaining -= len(clipped)

        ordered = tuple(
            selected[record.discord_message_id]
            for record in records
            if record.discord_message_id in selected
        )
        return ChatContext(
            trigger_message_id=trigger_message_id,
            messages=ordered,
            character_count=sum(len(message.content) for message in ordered),
        )

    @staticmethod
    def _priority_order(
        trigger: MessageRecord,
        records: list[MessageRecord],
        by_message_id: dict[str, MessageRecord],
    ) -> tuple[MessageRecord, ...]:
        chain: list[MessageRecord] = []
        seen: set[str] = set()
        current: MessageRecord | None = trigger
        while current is not None and current.discord_message_id not in seen:
            chain.append(current)
            seen.add(current.discord_message_id)
            current = (
                by_message_id.get(current.replied_to_message_id)
                if current.replied_to_message_id is not None
                else None
            )
        recent = [record for record in reversed(records) if record.discord_message_id not in seen]
        return tuple([*chain, *recent])

    @staticmethod
    def _render(record: MessageRecord) -> str:
        author = record.author_display_name or f"使用者 {record.author_id}"
        return f"{author}: {record.content.strip()}"
