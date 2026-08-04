"""明確日期、時間與 IANA 時區的保守提醒服務。"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.security.sensitive_filter import SensitiveFilter
from app.storage.reminders import Reminder, ReminderRepository

MAX_REMINDER_CHARACTERS = 500
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


class InvalidReminderError(ValueError):
    """提醒內容、日期、時間或時區不符合保守格式。"""


class SensitiveReminderError(ValueError):
    """提醒內容可能含祕密，因此拒絕保存。"""


class ReminderService:
    """建立、顯示與取消目前使用者自己的提醒。"""

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
        self,
        *,
        guild_id: str,
        user_id: str,
        timezone_name: str,
    ) -> str:
        """驗證並保存目前使用者在此伺服器的 IANA 時區。"""

        validated = self.validate_timezone(timezone_name)
        await self._repository.set_timezone(
            guild_id=guild_id,
            user_id=user_id,
            timezone_name=validated,
        )
        return validated

    async def get_timezone(self, *, guild_id: str, user_id: str) -> str:
        """讀取使用者時區，未設定時使用 Asia/Taipei。"""

        return (
            await self._repository.get_timezone(guild_id=guild_id, user_id=user_id)
            or self._default_timezone
        )

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
        """以使用者時區將明確本地時間轉為 UTC 後保存。"""

        cleaned = re.sub(r"\s+", " ", content).strip()
        if not cleaned or len(cleaned) > MAX_REMINDER_CHARACTERS:
            raise InvalidReminderError(
                f"提醒內容必須介於 1 到 {MAX_REMINDER_CHARACTERS} 個字元"
            )
        if self._sensitive_filter.scan(cleaned).is_sensitive:
            raise SensitiveReminderError("提醒可能含敏感資料，未保存")
        timezone_name = await self.get_timezone(
            guild_id=guild_id,
            user_id=user_id,
        )
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

    async def list_own(
        self,
        *,
        guild_id: str,
        user_id: str,
    ) -> tuple[Reminder, ...]:
        """列出目前使用者尚未完成或取消的提醒。"""

        return await self._repository.list_own_pending(
            guild_id=guild_id,
            user_id=user_id,
        )

    async def cancel(
        self,
        *,
        guild_id: str,
        user_id: str,
        reminder_id: int,
    ) -> bool:
        """取消目前使用者自己的指定提醒。"""

        return await self._repository.cancel_own(
            guild_id=guild_id,
            user_id=user_id,
            reminder_id=reminder_id,
        )

    @staticmethod
    def validate_timezone(timezone_name: str) -> str:
        """只接受可由標準 tzdata 載入的 IANA 時區名稱。"""

        cleaned = timezone_name.strip()
        if not cleaned or len(cleaned) > 64:
            raise InvalidReminderError("時區名稱格式不正確")
        try:
            ZoneInfo(cleaned)
        except ZoneInfoNotFoundError as error:
            raise InvalidReminderError(
                "找不到這個 IANA 時區，例如可使用 Asia/Taipei"
            ) from error
        return cleaned

    @classmethod
    def parse_local_datetime(
        cls,
        *,
        date_text: str,
        time_text: str,
        timezone_name: str,
    ) -> datetime:
        """拒絕格式錯誤、夏令時間不存在或重複的本地時間。"""

        if not _DATE_PATTERN.fullmatch(date_text) or not _TIME_PATTERN.fullmatch(
            time_text
        ):
            raise InvalidReminderError("日期與時間必須使用 YYYY-MM-DD 和 HH:MM")
        try:
            local_date = date.fromisoformat(date_text)
            local_time = time.fromisoformat(time_text)
        except ValueError as error:
            raise InvalidReminderError("日期或時間不存在") from error
        zone = ZoneInfo(cls.validate_timezone(timezone_name))
        naive = datetime.combine(local_date, local_time)
        candidates: dict[float, datetime] = {}
        for fold in (0, 1):
            candidate = naive.replace(tzinfo=zone, fold=fold)
            round_trip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
            if round_trip == naive:
                candidates[candidate.timestamp()] = candidate
        if not candidates:
            raise InvalidReminderError("這個本地時間因時制調整而不存在")
        if len(candidates) > 1:
            raise InvalidReminderError("這個本地時間因時制調整而重複，請改用其他時間")
        return next(iter(candidates.values())).astimezone(UTC)

    @staticmethod
    def format_due_at(reminder: Reminder) -> str:
        """依建立提醒時的時區顯示明確日期、時間與時區。"""

        local = ReminderService._as_utc(reminder.due_at).astimezone(
            ZoneInfo(reminder.timezone_name)
        )
        return f"{local:%Y-%m-%d %H:%M} {reminder.timezone_name}"

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
