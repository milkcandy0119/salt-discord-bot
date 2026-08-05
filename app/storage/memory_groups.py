"""白名單與記憶分組的持久化規則。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import (
    ChannelAllowlistRecord,
    MemoryGroupChannelRecord,
    MemoryGroupRecord,
)


class MemoryGroupError(ValueError):
    """分組名稱、範圍或成員資格不符合規則。"""


@dataclass(frozen=True, slots=True)
class MemoryGroup:
    id: int
    guild_id: str
    name: str
    description: str
    channel_ids: tuple[str, ...]


class ChannelAccessRepository:
    """集中處理可接收頻道與記憶搜尋範圍。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def seed_allowlist(
        self, *, guild_ids: frozenset[int], channel_ids: frozenset[int]
    ) -> None:
        """只在首次啟用時將既有 .env 頻道移入資料庫。

        後續以資料庫為唯一真實來源，避免重啟時復活管理員已移除的頻道。
        """

        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            if await session.scalar(select(ChannelAllowlistRecord.id).limit(1)) is not None:
                return
            for guild_id in guild_ids:
                for channel_id in channel_ids:
                    await session.execute(
                        sqlite_insert(ChannelAllowlistRecord)
                        .values(
                            guild_id=str(guild_id),
                            channel_id=str(channel_id),
                            created_at=now,
                            updated_at=now,
                        )
                        .on_conflict_do_nothing(index_elements=["guild_id", "channel_id"])
                    )

    async def is_allowed(self, *, guild_id: str, channel_id: str) -> bool:
        async with self._session_factory() as session:
            value = await session.scalar(
                select(ChannelAllowlistRecord.id)
                .where(
                    ChannelAllowlistRecord.guild_id == guild_id,
                    ChannelAllowlistRecord.channel_id == channel_id,
                )
                .limit(1)
            )
        return value is not None

    async def list_allowed(self, *, guild_id: str) -> tuple[str, ...]:
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(ChannelAllowlistRecord.channel_id)
                    .where(ChannelAllowlistRecord.guild_id == guild_id)
                    .order_by(ChannelAllowlistRecord.channel_id)
                )
            ).all()
        return tuple(rows)

    async def add_allowed(self, *, guild_id: str, channel_id: str) -> bool:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                sqlite_insert(ChannelAllowlistRecord)
                .values(guild_id=guild_id, channel_id=channel_id, created_at=now, updated_at=now)
                .on_conflict_do_nothing(index_elements=["guild_id", "channel_id"])
            )
        return result.rowcount == 1

    async def remove_allowed(self, *, guild_id: str, channel_id: str) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(ChannelAllowlistRecord).where(
                    ChannelAllowlistRecord.guild_id == guild_id,
                    ChannelAllowlistRecord.channel_id == channel_id,
                )
            )
        return result.rowcount == 1

    async def create_group(self, *, guild_id: str, name: str, description: str = "") -> MemoryGroup:
        cleaned_name = self._clean_name(name)
        cleaned_description = " ".join(description.split())[:500]
        now = datetime.now(UTC)
        try:
            async with self._session_factory() as session, session.begin():
                record = MemoryGroupRecord(
                    guild_id=guild_id,
                    name=cleaned_name,
                    description=cleaned_description,
                    created_at=now,
                    updated_at=now,
                )
                session.add(record)
                await session.flush()
                result = MemoryGroup(
                    record.id,
                    record.guild_id,
                    record.name,
                    record.description,
                    (),
                )
        except IntegrityError as error:
            raise MemoryGroupError("這個伺服器已有相同名稱的記憶分組") from error
        return result

    async def list_groups(self, *, guild_id: str) -> tuple[MemoryGroup, ...]:
        async with self._session_factory() as session:
            groups = (
                await session.scalars(
                    select(MemoryGroupRecord)
                    .where(MemoryGroupRecord.guild_id == guild_id)
                    .order_by(MemoryGroupRecord.name)
                )
            ).all()
            members = (
                await session.execute(
                    select(
                        MemoryGroupChannelRecord.group_id, MemoryGroupChannelRecord.channel_id
                    ).where(MemoryGroupChannelRecord.guild_id == guild_id)
                )
            ).all()
        member_map: dict[int, list[str]] = {}
        for group_id, channel_id in members:
            member_map.setdefault(group_id, []).append(channel_id)
        return tuple(
            MemoryGroup(
                group.id,
                group.guild_id,
                group.name,
                group.description,
                tuple(sorted(member_map.get(group.id, []))),
            )
            for group in groups
        )

    async def add_channel(self, *, guild_id: str, group_name: str, channel_id: str) -> None:
        group = await self._group_by_name(guild_id=guild_id, name=group_name)
        if group is None:
            raise MemoryGroupError("找不到這個記憶分組")
        if not await self.is_allowed(guild_id=guild_id, channel_id=channel_id):
            raise MemoryGroupError("頻道尚未列入白名單，不能加入記憶分組")
        try:
            async with self._session_factory() as session, session.begin():
                session.add(
                    MemoryGroupChannelRecord(
                        group_id=group.id,
                        guild_id=guild_id,
                        channel_id=channel_id,
                        created_at=datetime.now(UTC),
                    )
                )
        except IntegrityError as error:
            raise MemoryGroupError("這個頻道已經加入其他記憶分組") from error

    async def remove_channel(self, *, guild_id: str, group_name: str, channel_id: str) -> bool:
        group = await self._group_by_name(guild_id=guild_id, name=group_name)
        if group is None:
            raise MemoryGroupError("找不到這個記憶分組")
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(MemoryGroupChannelRecord).where(
                    MemoryGroupChannelRecord.group_id == group.id,
                    MemoryGroupChannelRecord.guild_id == guild_id,
                    MemoryGroupChannelRecord.channel_id == channel_id,
                )
            )
        return result.rowcount == 1

    async def delete_group(self, *, guild_id: str, name: str) -> bool:
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                delete(MemoryGroupRecord).where(
                    MemoryGroupRecord.guild_id == guild_id,
                    MemoryGroupRecord.name == self._clean_name(name),
                )
            )
        return result.rowcount == 1

    async def edit_group(
        self, *, guild_id: str, name: str, new_name: str | None, description: str | None
    ) -> MemoryGroup | None:
        group = await self._group_by_name(guild_id=guild_id, name=name)
        if group is None:
            return None
        values: dict[str, object] = {"updated_at": datetime.now(UTC)}
        if new_name is not None:
            values["name"] = self._clean_name(new_name)
        if description is not None:
            values["description"] = " ".join(description.split())[:500]
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    update(MemoryGroupRecord)
                    .where(MemoryGroupRecord.id == group.id)
                    .values(**values)
                )
        except IntegrityError as error:
            raise MemoryGroupError("這個伺服器已有相同名稱的記憶分組") from error
        groups = await self.list_groups(guild_id=guild_id)
        return next(item for item in groups if item.id == group.id)

    async def visible_channel_ids(self, *, guild_id: str, channel_id: str) -> tuple[str, ...]:
        """未分組頻道只看自己；已分組頻道只看同組且同 guild 的頻道。"""

        async with self._session_factory() as session:
            group_id = await session.scalar(
                select(MemoryGroupChannelRecord.group_id).where(
                    MemoryGroupChannelRecord.guild_id == guild_id,
                    MemoryGroupChannelRecord.channel_id == channel_id,
                )
            )
            if group_id is None:
                return (channel_id,)
            rows = (
                await session.scalars(
                    select(MemoryGroupChannelRecord.channel_id)
                    .where(
                        MemoryGroupChannelRecord.guild_id == guild_id,
                        MemoryGroupChannelRecord.group_id == group_id,
                    )
                    .order_by(MemoryGroupChannelRecord.channel_id)
                )
            ).all()
        return tuple(rows)

    async def _group_by_name(self, *, guild_id: str, name: str) -> MemoryGroupRecord | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(MemoryGroupRecord).where(
                    MemoryGroupRecord.guild_id == guild_id,
                    MemoryGroupRecord.name == self._clean_name(name),
                )
            )

    @staticmethod
    def _clean_name(value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned or len(cleaned) > 100:
            raise MemoryGroupError("分組名稱必須介於 1 到 100 個字元")
        return cleaned
