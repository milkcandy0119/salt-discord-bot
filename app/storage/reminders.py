"""持久化提醒、時區與可恢復派送狀態的資料存取。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import ReminderRecord, UserTimezoneRecord


@dataclass(frozen=True, slots=True)
class Reminder:
    """不依賴已關閉 session 的提醒檢視。"""

    id: int
    guild_id: str
    user_id: str
    content: str
    timezone_name: str
    due_at: datetime
    status: str
    attempts: int
    max_attempts: int
    last_error_code: str | None


class ReminderRepository:
    """提供本人隔離、原子 claim 及重啟恢復。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def set_timezone(
        self,
        *,
        guild_id: str,
        user_id: str,
        timezone_name: str,
        now: datetime | None = None,
    ) -> None:
        """以 guild/user 唯一鍵建立或更新使用者時區。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sqlite_insert(UserTimezoneRecord)
                .values(
                    guild_id=guild_id,
                    user_id=user_id,
                    timezone_name=timezone_name,
                    created_at=effective_now,
                    updated_at=effective_now,
                )
                .on_conflict_do_update(
                    index_elements=["guild_id", "user_id"],
                    set_={
                        "timezone_name": timezone_name,
                        "updated_at": effective_now,
                    },
                )
            )

    async def get_timezone(
        self,
        *,
        guild_id: str,
        user_id: str,
    ) -> str | None:
        """讀取目前使用者在此伺服器設定的時區。"""

        async with self._session_factory() as session:
            return await session.scalar(
                select(UserTimezoneRecord.timezone_name).where(
                    UserTimezoneRecord.guild_id == guild_id,
                    UserTimezoneRecord.user_id == user_id,
                )
            )

    async def has_timezone(self, *, guild_id: str, user_id: str) -> bool:
        """提醒建立流程可用此方法分辨預設值與使用者明確設定。"""

        return await self.get_timezone(guild_id=guild_id, user_id=user_id) is not None

    async def create(
        self,
        *,
        guild_id: str,
        user_id: str,
        content: str,
        timezone_name: str,
        due_at: datetime,
        max_attempts: int,
        now: datetime | None = None,
    ) -> Reminder:
        """建立不呼叫 AI 的持久化私訊提醒。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            record = ReminderRecord(
                guild_id=guild_id,
                user_id=user_id,
                content=content,
                timezone_name=timezone_name,
                due_at=due_at,
                status="pending",
                attempts=0,
                max_attempts=max_attempts,
                available_at=effective_now,
                created_at=effective_now,
                updated_at=effective_now,
            )
            session.add(record)
            await session.flush()
            reminder = self._to_reminder(record)
        return reminder

    async def list_own_pending(
        self,
        *,
        guild_id: str,
        user_id: str,
        limit: int = 50,
    ) -> tuple[Reminder, ...]:
        """只列出目前使用者尚未結束的提醒。"""

        async with self._session_factory() as session:
            records = (
                await session.scalars(
                    select(ReminderRecord)
                    .where(
                        ReminderRecord.guild_id == guild_id,
                        ReminderRecord.user_id == user_id,
                        ReminderRecord.status.in_(
                            ("pending", "sending", "retry_wait")
                        ),
                    )
                    .order_by(ReminderRecord.due_at, ReminderRecord.id)
                    .limit(limit)
                )
            ).all()
        return tuple(self._to_reminder(record) for record in records)

    async def cancel_own(
        self,
        *,
        guild_id: str,
        user_id: str,
        reminder_id: int,
        now: datetime | None = None,
    ) -> bool:
        """只有擁有者可以取消尚未被派送器領取的提醒。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(ReminderRecord)
                .where(
                    ReminderRecord.id == reminder_id,
                    ReminderRecord.guild_id == guild_id,
                    ReminderRecord.user_id == user_id,
                    ReminderRecord.status.in_(("pending", "retry_wait")),
                )
                .values(
                    status="cancelled",
                    cancelled_at=effective_now,
                    updated_at=effective_now,
                )
            )
        return result.rowcount == 1

    async def claim_due(
        self,
        *,
        stale_after: timedelta,
        now: datetime | None = None,
    ) -> Reminder | None:
        """原子領取一筆到期提醒，並回收重啟前卡住的 sending。"""

        effective_now = now or datetime.now(UTC)
        ready_statuses = ("pending", "retry_wait")
        candidate = (
            select(ReminderRecord.id)
            .where(
                ReminderRecord.status.in_(ready_statuses),
                ReminderRecord.due_at <= effective_now,
                ReminderRecord.available_at <= effective_now,
            )
            .order_by(ReminderRecord.due_at, ReminderRecord.id)
            .limit(1)
            .scalar_subquery()
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(ReminderRecord)
                .where(
                    ReminderRecord.status == "sending",
                    ReminderRecord.claimed_at < effective_now - stale_after,
                )
                .values(
                    status="retry_wait",
                    claimed_at=None,
                    available_at=effective_now,
                    last_error_code="stale_claim_recovered",
                    updated_at=effective_now,
                )
            )
            record = (
                await session.execute(
                    update(ReminderRecord)
                    .where(
                        ReminderRecord.id == candidate,
                        ReminderRecord.status.in_(ready_statuses),
                        ReminderRecord.due_at <= effective_now,
                        ReminderRecord.available_at <= effective_now,
                    )
                    .values(
                        status="sending",
                        attempts=ReminderRecord.attempts + 1,
                        claimed_at=effective_now,
                        last_error_code=None,
                        updated_at=effective_now,
                    )
                    .returning(ReminderRecord)
                )
            ).scalar_one_or_none()
        return self._to_reminder(record) if record is not None else None

    async def mark_sent(
        self,
        reminder_id: int,
        *,
        now: datetime | None = None,
    ) -> None:
        """將成功私訊的提醒標為 sent。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(ReminderRecord)
                .where(
                    ReminderRecord.id == reminder_id,
                    ReminderRecord.status == "sending",
                )
                .values(
                    status="sent",
                    claimed_at=None,
                    sent_at=effective_now,
                    updated_at=effective_now,
                )
            )

    async def retry_or_fail(
        self,
        reminder: Reminder,
        *,
        error_code: str,
        retryable: bool,
        base_delay: timedelta,
        now: datetime | None = None,
    ) -> str:
        """保留失敗提醒；可重試錯誤採有上限的指數退避。"""

        effective_now = now or datetime.now(UTC)
        failed = not retryable or reminder.attempts >= reminder.max_attempts
        exponent = max(reminder.attempts - 1, 0)
        delay_seconds = min(
            base_delay.total_seconds() * (2**exponent),
            timedelta(hours=1).total_seconds(),
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(ReminderRecord)
                .where(
                    ReminderRecord.id == reminder.id,
                    ReminderRecord.status == "sending",
                )
                .values(
                    status="failed" if failed else "retry_wait",
                    available_at=effective_now + timedelta(seconds=delay_seconds),
                    claimed_at=None,
                    last_error_code=error_code[:64],
                    updated_at=effective_now,
                )
            )
        return "failed" if failed else "retry_wait"

    async def status_counts(self) -> dict[str, int]:
        """回傳不含提醒內容的狀態統計。"""

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(ReminderRecord.status, func.count())
                    .group_by(ReminderRecord.status)
                    .order_by(ReminderRecord.status)
                )
            ).all()
        return {status: int(count) for status, count in rows}

    @staticmethod
    def _to_reminder(record: ReminderRecord) -> Reminder:
        return Reminder(
            id=record.id,
            guild_id=record.guild_id,
            user_id=record.user_id,
            content=record.content,
            timezone_name=record.timezone_name,
            due_at=record.due_at,
            status=record.status,
            attempts=record.attempts,
            max_attempts=record.max_attempts,
            last_error_code=record.last_error_code,
        )
