"""一次性待確認動作的持久化與原子領取。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import PendingActionRecord


@dataclass(frozen=True, slots=True)
class PendingAction:
    id: int
    guild_id: str
    channel_id: str
    user_id: str
    action_type: str
    parsed_parameters: dict[str, str]
    expires_at: datetime
    status: str


class PendingActionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        *,
        guild_id: str,
        channel_id: str,
        user_id: str,
        action_type: str,
        parsed_parameters: dict[str, str],
        expires_after: timedelta = timedelta(minutes=15),
        now: datetime | None = None,
    ) -> PendingAction:
        effective_now = now or datetime.now(UTC)
        if expires_after <= timedelta(0):
            raise ValueError("待確認動作有效期限必須大於零")
        async with self._session_factory() as session, session.begin():
            record = PendingActionRecord(
                guild_id=guild_id, channel_id=channel_id, user_id=user_id,
                action_type=action_type, parsed_parameters=parsed_parameters,
                expires_at=effective_now + expires_after, status="pending",
                created_at=effective_now, updated_at=effective_now,
            )
            session.add(record)
            await session.flush()
            return self._to_action(record)

    async def claim_for_execution(
        self, *, action_id: int, guild_id: str, channel_id: str, user_id: str,
        now: datetime | None = None,
    ) -> PendingAction | None:
        """僅能由原上下文的使用者領取一次；過期動作不會執行。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            record = (await session.execute(
                update(PendingActionRecord)
                .where(
                    PendingActionRecord.id == action_id,
                    PendingActionRecord.guild_id == guild_id,
                    PendingActionRecord.channel_id == channel_id,
                    PendingActionRecord.user_id == user_id,
                    PendingActionRecord.status == "pending",
                    PendingActionRecord.expires_at > effective_now,
                )
                .values(status="executing", updated_at=effective_now)
                .returning(PendingActionRecord)
            )).scalar_one_or_none()
        return self._to_action(record) if record is not None else None

    async def finish(self, *, action_id: int, succeeded: bool, now: datetime | None = None) -> None:
        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(PendingActionRecord)
                .where(
                    PendingActionRecord.id == action_id,
                    PendingActionRecord.status == "executing",
                )
                .values(
                    status="completed" if succeeded else "failed",
                    updated_at=effective_now,
                )
            )

    @staticmethod
    def _to_action(record: PendingActionRecord) -> PendingAction:
        return PendingAction(
            record.id, record.guild_id, record.channel_id, record.user_id,
            record.action_type, dict(record.parsed_parameters), record.expires_at, record.status,
        )
