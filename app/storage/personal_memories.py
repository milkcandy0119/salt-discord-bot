"""個人記憶的擁有者隔離與冪等資料存取。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import PersonalMemoryRecord


class MemoryConflictError(RuntimeError):
    """同一位使用者已存在相同內容的記憶。"""


@dataclass(frozen=True, slots=True)
class PersonalMemory:
    """不依賴已關閉 session 的個人記憶檢視。"""

    id: int
    guild_id: str
    user_id: str
    content: str
    source_type: str
    source_message_id: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class MemorySaveResult:
    """建立記憶的結果；重送同一事件時會回傳既有資料。"""

    memory: PersonalMemory
    created: bool


class PersonalMemoryRepository:
    """所有修改都同時比對 guild ID 與目前使用者 ID。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        guild_id: str,
        user_id: str,
        content: str,
        source_type: str,
        source_message_id: str | None = None,
        now: datetime | None = None,
    ) -> MemorySaveResult:
        """以來源訊息及正規化內容冪等建立個人記憶。"""

        effective_now = now or datetime.now(UTC)
        normalized = self.normalize(content)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                sqlite_insert(PersonalMemoryRecord)
                .values(
                    guild_id=guild_id,
                    user_id=user_id,
                    content=content.strip(),
                    normalized_content=normalized,
                    source_type=source_type,
                    source_message_id=source_message_id,
                    created_at=effective_now,
                    updated_at=effective_now,
                )
                .on_conflict_do_nothing()
            )
            record = await session.scalar(
                select(PersonalMemoryRecord).where(
                    PersonalMemoryRecord.guild_id == guild_id,
                    PersonalMemoryRecord.user_id == user_id,
                    PersonalMemoryRecord.normalized_content == normalized,
                )
            )
            if record is None and source_message_id is not None:
                record = await session.scalar(
                    select(PersonalMemoryRecord).where(
                        PersonalMemoryRecord.source_message_id == source_message_id,
                        PersonalMemoryRecord.guild_id == guild_id,
                        PersonalMemoryRecord.user_id == user_id,
                    )
                )
        if record is None:
            raise RuntimeError("個人記憶保存後無法讀回")
        return MemorySaveResult(self._to_memory(record), result.rowcount == 1)

    async def list_for_user(
        self,
        *,
        guild_id: str,
        user_id: str,
        limit: int = 50,
    ) -> tuple[PersonalMemory, ...]:
        """只列出指定伺服器中屬於該使用者的記憶。"""

        if limit <= 0:
            return ()
        async with self._session_factory() as session:
            records = (
                await session.scalars(
                    select(PersonalMemoryRecord)
                    .where(
                        PersonalMemoryRecord.guild_id == guild_id,
                        PersonalMemoryRecord.user_id == user_id,
                    )
                    .order_by(
                        PersonalMemoryRecord.updated_at.desc(),
                        PersonalMemoryRecord.id.desc(),
                    )
                    .limit(limit)
                )
            ).all()
        return tuple(self._to_memory(record) for record in records)

    async def update_own(
        self,
        *,
        guild_id: str,
        user_id: str,
        memory_id: int,
        content: str,
        now: datetime | None = None,
    ) -> PersonalMemory | None:
        """只修改目前使用者自己的指定記憶。"""

        normalized = self.normalize(content)
        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            duplicate_id = await session.scalar(
                select(PersonalMemoryRecord.id).where(
                    PersonalMemoryRecord.guild_id == guild_id,
                    PersonalMemoryRecord.user_id == user_id,
                    PersonalMemoryRecord.normalized_content == normalized,
                    PersonalMemoryRecord.id != memory_id,
                )
            )
            if duplicate_id is not None:
                raise MemoryConflictError("已存在相同內容的記憶")
            record = (
                await session.execute(
                    update(PersonalMemoryRecord)
                    .where(
                        PersonalMemoryRecord.id == memory_id,
                        PersonalMemoryRecord.guild_id == guild_id,
                        PersonalMemoryRecord.user_id == user_id,
                    )
                    .values(
                        content=content.strip(),
                        normalized_content=normalized,
                        source_type="slash",
                        updated_at=effective_now,
                    )
                    .returning(PersonalMemoryRecord)
                )
            ).scalar_one_or_none()
        return self._to_memory(record) if record is not None else None

    async def delete_own(
        self,
        *,
        guild_id: str,
        user_id: str,
        memory_id: int,
    ) -> bool:
        """只刪除目前使用者自己的指定記憶。"""

        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(PersonalMemoryRecord).where(
                    PersonalMemoryRecord.id == memory_id,
                    PersonalMemoryRecord.guild_id == guild_id,
                    PersonalMemoryRecord.user_id == user_id,
                )
            )
        return result.rowcount == 1

    @staticmethod
    def normalize(content: str) -> str:
        """建立不受大小寫與多餘空白影響的重複判斷值。"""

        return re.sub(r"\s+", " ", content).strip().casefold()

    @staticmethod
    def _to_memory(record: PersonalMemoryRecord) -> PersonalMemory:
        return PersonalMemory(
            id=record.id,
            guild_id=record.guild_id,
            user_id=record.user_id,
            content=record.content,
            source_type=record.source_type,
            source_message_id=record.source_message_id,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
