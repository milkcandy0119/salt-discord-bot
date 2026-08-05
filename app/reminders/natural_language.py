"""不呼叫 AI 的保守中文自然語言提醒時間解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.reminders.service import InvalidReminderError

_RELATIVE_PATTERN = re.compile(r"(?P<minutes>\d{1,4})\s*分鐘後")
_DATE_TIME_PATTERN = re.compile(
    r"(?P<day>明天|後天|下週[一二三四五六日天])\s*"
    r"(?P<period>早上|上午|下午|晚上)?\s*"
    r"(?P<hour>\d{1,2})\s*點\s*(?P<minute>\d{1,2}|半)?"
)
_WEEKDAY = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


@dataclass(frozen=True, slots=True)
class ParsedReminderRequest:
    date_text: str
    time_text: str
    content: str
    local_display: str


class NaturalLanguageReminderParser:
    """只接受規格列出的明確時間；模糊句子一律拒絕猜測。"""

    def parse(
        self, *, text: str, timezone_name: str, now: datetime | None = None
    ) -> ParsedReminderRequest:
        clean = " ".join(text.split())
        local_now = (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone_name))
        match = _RELATIVE_PATTERN.search(clean)
        if match is not None:
            target = local_now + timedelta(minutes=int(match["minutes"]))
            return self._result(clean, match, target)
        match = _DATE_TIME_PATTERN.search(clean)
        if match is None:
            raise InvalidReminderError("請提供明確時間，例如「明天晚上 8 點」或「30 分鐘後」")
        hour = int(match["hour"])
        minute_text = match["minute"]
        minute = 30 if minute_text == "半" else int(minute_text or 0)
        period = match["period"]
        if period in {"下午", "晚上"} and 1 <= hour <= 11:
            hour += 12
        if period in {"早上", "上午"} and hour == 12:
            hour = 0
        if hour > 23 or minute > 59:
            raise InvalidReminderError("時間格式不正確")
        day = match["day"]
        if day == "明天":
            target_date = local_now.date() + timedelta(days=1)
        elif day == "後天":
            target_date = local_now.date() + timedelta(days=2)
        else:
            days_until = 7 - local_now.weekday() + _WEEKDAY[day[-1]]
            target_date = local_now.date() + timedelta(days=days_until)
        target = datetime.combine(
            target_date, datetime.min.time(), tzinfo=local_now.tzinfo
        ).replace(
            hour=hour, minute=minute
        )
        return self._result(clean, match, target)

    @staticmethod
    def _result(text: str, match: re.Match[str], target: datetime) -> ParsedReminderRequest:
        content = text[:match.start()] + text[match.end():]
        content = re.sub(r"^(?:提醒我|提醒|請|幫我)\s*", "", content.strip())
        content = re.sub(r"^(?:在)?\s*", "", content).strip(" ，。！!：:")
        if not content:
            raise InvalidReminderError("請補上提醒內容")
        return ParsedReminderRequest(
            date_text=f"{target:%Y-%m-%d}",
            time_text=f"{target:%H:%M}",
            content=content,
            local_display=f"{target:%Y-%m-%d %H:%M} {target.tzinfo.key}",
        )
