"""Reminder application service, including timezone and recurrence validation."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.reminders.schedule import (
    InvalidScheduleError,
    next_due_at,
    parse_interval_days,
    parse_local_datetime,
    parse_start_date,
    parse_weekdays,
    validate_timezone,
)
from app.security.sensitive_filter import SensitiveFilter
from app.storage.reminders import Reminder, ReminderRepository

MAX_REMINDER_CHARACTERS = 500


class InvalidReminderError(ValueError):
    """The user supplied invalid reminder data."""


class SensitiveReminderError(ValueError):
    """The reminder contains information that must not be persisted."""


class ReminderService:
    """Create, display, and cancel private reminders for their owner only."""

    def __init__(
        self,
        repository: ReminderRepository,
        *,
        sensitive_filter: SensitiveFilter,
        default_timezone: str = "Asia/Taipei",
        max_attempts: int = 5,
    ) -> None:
        self._repository = repository
        self._sensitive_filter = sensitive_filter
        self._default_timezone = self.validate_timezone(default_timezone)
        self._max_attempts = max_attempts

    async def set_timezone(
        self, *, guild_id: str, user_id: str, timezone_name: str
    ) -> str:
        validated = self.validate_timezone(timezone_name)
        await self._repository.set_timezone(
            guild_id=guild_id,
            user_id=user_id,
            timezone_name=validated,
        )
        return validated

    async def get_timezone(self, *, guild_id: str, user_id: str) -> str:
        return (
            await self._repository.get_timezone(guild_id=guild_id, user_id=user_id)
            or self._default_timezone
        )

    async def require_timezone(self, *, guild_id: str, user_id: str) -> str:
        timezone_name = await self._repository.get_timezone(
            guild_id=guild_id, user_id=user_id
        )
        if timezone_name is None:
            raise InvalidReminderError("請先使用 /timezone set 設定你的時區")
        return timezone_name

    async def create(
        self,
        *,
        guild_id: str,
        user_id: str,
        date_text: str,
        time_text: str,
        content: str,
        now: datetime | None = None,
    ) -> Reminder:
        """Create a one-time reminder; kept as the service compatibility API."""

        cleaned = self._validate_content(content)
        timezone_name = await self.require_timezone(guild_id=guild_id, user_id=user_id)
        due_at = self.parse_local_datetime(
            date_text=date_text,
            time_text=time_text,
            timezone_name=timezone_name,
        )
        effective_now = self._as_utc(now or datetime.now(UTC))
        if due_at <= effective_now:
            raise InvalidReminderError("提醒時間必須晚於目前時間")
        return await self._repository.create(
            guild_id=guild_id,
            user_id=user_id,
            content=cleaned,
            timezone_name=timezone_name,
            due_at=due_at,
            max_attempts=self._max_attempts,
            now=effective_now,
        )

    async def create_daily(
        self,
        *,
        guild_id: str,
        user_id: str,
        time_text: str,
        content: str,
        now: datetime | None = None,
    ) -> Reminder:
        return await self._create_recurring(
            guild_id=guild_id,
            user_id=user_id,
            recurrence_kind="daily",
            time_text=time_text,
            content=content,
            now=now,
        )

    async def create_weekly(
        self,
        *,
        guild_id: str,
        user_id: str,
        weekdays_text: str,
        time_text: str,
        content: str,
        now: datetime | None = None,
    ) -> Reminder:
        try:
            weekdays = parse_weekdays(weekdays_text)
        except InvalidScheduleError as error:
            raise InvalidReminderError(str(error)) from error
        return await self._create_recurring(
            guild_id=guild_id,
            user_id=user_id,
            recurrence_kind="weekly",
            time_text=time_text,
            content=content,
            weekdays=weekdays,
            now=now,
        )

    async def create_interval(
        self,
        *,
        guild_id: str,
        user_id: str,
        every_text: str,
        start_date_text: str,
        time_text: str,
        content: str,
        now: datetime | None = None,
    ) -> Reminder:
        try:
            interval_days = parse_interval_days(every_text)
            start_date = parse_start_date(start_date_text)
        except InvalidScheduleError as error:
            raise InvalidReminderError(str(error)) from error
        return await self._create_recurring(
            guild_id=guild_id,
            user_id=user_id,
            recurrence_kind="interval",
            time_text=time_text,
            content=content,
            interval_days=interval_days,
            start_date=start_date,
            now=now,
        )

    async def list_own(
        self,
        *,
        guild_id: str,
        user_id: str,
        limit: int = 50,
    ) -> tuple[Reminder, ...]:
        return await self._repository.list_own_pending(
            guild_id=guild_id,
            user_id=user_id,
            limit=limit,
        )

    async def cancel(self, *, guild_id: str, user_id: str, reminder_id: int) -> bool:
        return await self._repository.cancel_own(
            guild_id=guild_id,
            user_id=user_id,
            reminder_id=reminder_id,
        )

    async def cancel_many(
        self,
        *,
        guild_id: str,
        user_id: str,
        reminder_ids: tuple[int, ...],
    ) -> int:
        validated_ids = self._validate_reminder_ids(reminder_ids)
        return await self._repository.cancel_many_own(
            guild_id=guild_id,
            user_id=user_id,
            reminder_ids=validated_ids,
        )

    async def update_many_content(
        self,
        *,
        guild_id: str,
        user_id: str,
        reminder_ids: tuple[int, ...],
        content: str,
    ) -> int:
        validated_ids = self._validate_reminder_ids(reminder_ids)
        cleaned = self._validate_content(content)
        return await self._repository.update_many_own_content(
            guild_id=guild_id,
            user_id=user_id,
            reminder_ids=validated_ids,
            content=cleaned,
        )

    @staticmethod
    def validate_timezone(timezone_name: str) -> str:
        try:
            return validate_timezone(timezone_name)
        except InvalidScheduleError as error:
            raise InvalidReminderError(str(error)) from error

    @staticmethod
    def parse_local_datetime(
        *, date_text: str, time_text: str, timezone_name: str
    ) -> datetime:
        try:
            return parse_local_datetime(
                date_text=date_text,
                time_text=time_text,
                timezone_name=timezone_name,
            )
        except InvalidScheduleError as error:
            raise InvalidReminderError(str(error)) from error

    @staticmethod
    def format_due_at(reminder: Reminder) -> str:
        local = ReminderService._as_utc(reminder.due_at).astimezone(
            ZoneInfo(reminder.timezone_name)
        )
        return f"{local:%Y-%m-%d %H:%M} {reminder.timezone_name}"

    @staticmethod
    def format_recurrence(reminder: Reminder) -> str:
        if reminder.recurrence_kind == "daily":
            return "每天"
        if reminder.recurrence_kind == "weekly":
            labels = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")
            return "每週 " + "、".join(labels[day] for day in reminder.recurrence_weekdays)
        if reminder.recurrence_kind == "interval":
            return f"每 {reminder.interval_days} 天"
        return "一次"

    async def _create_recurring(
        self,
        *,
        guild_id: str,
        user_id: str,
        recurrence_kind: str,
        time_text: str,
        content: str,
        weekdays: tuple[int, ...] = (),
        interval_days: int | None = None,
        start_date: date | None = None,
        now: datetime | None = None,
    ) -> Reminder:
        cleaned = self._validate_content(content)
        timezone_name = await self.require_timezone(guild_id=guild_id, user_id=user_id)
        effective_now = self._as_utc(now or datetime.now(UTC))
        try:
            due_at = next_due_at(
                recurrence_kind=recurrence_kind,
                timezone_name=timezone_name,
                time_text=time_text,
                reference=effective_now,
                weekdays=weekdays,
                interval_days=interval_days,
                start_date=start_date,
            )
        except InvalidScheduleError as error:
            raise InvalidReminderError(str(error)) from error
        return await self._repository.create(
            guild_id=guild_id,
            user_id=user_id,
            content=cleaned,
            timezone_name=timezone_name,
            due_at=due_at,
            max_attempts=self._max_attempts,
            recurrence_kind=recurrence_kind,
            recurrence_time=time_text,
            recurrence_weekdays=weekdays,
            interval_days=interval_days,
            recurrence_start_date=start_date.isoformat() if start_date else None,
            now=effective_now,
        )

    def _validate_content(self, content: str) -> str:
        cleaned = re.sub(r"\s+", " ", content).strip()
        if not cleaned or len(cleaned) > MAX_REMINDER_CHARACTERS:
            raise InvalidReminderError(
                f"提醒內容長度必須介於 1 到 {MAX_REMINDER_CHARACTERS} 個字元"
            )
        if self._sensitive_filter.scan(cleaned).is_sensitive:
            raise SensitiveReminderError("提醒內容包含疑似敏感資訊，無法儲存")
        return cleaned

    @staticmethod
    def _validate_reminder_ids(reminder_ids: tuple[int, ...]) -> tuple[int, ...]:
        unique_ids = tuple(dict.fromkeys(reminder_ids))
        if (
            not unique_ids
            or len(unique_ids) > 25
            or any(reminder_id <= 0 for reminder_id in unique_ids)
        ):
            raise InvalidReminderError("請選擇 1 到 25 個有效的提醒")
        return unique_ids

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
