"""Pure validation and scheduling helpers for recurring reminders."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
_INTERVAL_PATTERN = re.compile(r"([1-9]\d{0,3})d", re.IGNORECASE)
_WEEKDAYS = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


class InvalidScheduleError(ValueError):
    """The supplied reminder schedule cannot be interpreted safely."""


def validate_timezone(timezone_name: str) -> str:
    cleaned = timezone_name.strip()
    if not cleaned or len(cleaned) > 64:
        raise InvalidScheduleError("時區格式不正確")
    try:
        ZoneInfo(cleaned)
    except ZoneInfoNotFoundError as error:
        raise InvalidScheduleError(
            "找不到這個 IANA 時區，例如可使用 Asia/Taipei"
        ) from error
    return cleaned


def parse_local_datetime(*, date_text: str, time_text: str, timezone_name: str) -> datetime:
    """Convert an unambiguous local date and time into UTC."""

    if not _DATE_PATTERN.fullmatch(date_text) or not _TIME_PATTERN.fullmatch(time_text):
        raise InvalidScheduleError("日期與時間必須分別使用 YYYY-MM-DD 與 HH:MM")
    try:
        local_date = date.fromisoformat(date_text)
        local_time = time.fromisoformat(time_text)
    except ValueError as error:
        raise InvalidScheduleError("日期或時間不是有效的日曆時間") from error
    zone = ZoneInfo(validate_timezone(timezone_name))
    naive = datetime.combine(local_date, local_time)
    candidates: dict[float, datetime] = {}
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if round_trip == naive:
            candidates[candidate.timestamp()] = candidate
    if not candidates:
        raise InvalidScheduleError("此本地時間在該時區的日光節約轉換中不存在")
    if len(candidates) > 1:
        raise InvalidScheduleError("此本地時間在該時區有兩個可能時間，請改用其他時間")
    return next(iter(candidates.values())).astimezone(UTC)


def parse_weekdays(weekdays_text: str) -> tuple[int, ...]:
    values = tuple(part.strip().casefold() for part in weekdays_text.split(",") if part.strip())
    if not values or len(values) > 7 or any(value not in _WEEKDAYS for value in values):
        raise InvalidScheduleError("星期請使用 mon,tue,wed,thu,fri,sat,sun，以逗號分隔")
    return tuple(sorted({_WEEKDAYS[value] for value in values}))


def parse_interval_days(every_text: str) -> int:
    matched = _INTERVAL_PATTERN.fullmatch(every_text.strip())
    if matched is None:
        raise InvalidScheduleError("間隔請使用正整數天數，例如 3d")
    return int(matched.group(1))


def parse_start_date(start_date_text: str) -> date:
    if not _DATE_PATTERN.fullmatch(start_date_text):
        raise InvalidScheduleError("起始日期必須使用 YYYY-MM-DD")
    try:
        return date.fromisoformat(start_date_text)
    except ValueError as error:
        raise InvalidScheduleError("起始日期不是有效日期") from error


def next_due_at(
    *,
    recurrence_kind: str,
    timezone_name: str,
    time_text: str,
    reference: datetime,
    weekdays: tuple[int, ...] = (),
    interval_days: int | None = None,
    start_date: date | None = None,
) -> datetime:
    """Return the first scheduled occurrence strictly after ``reference``.

    Ambiguous or nonexistent daylight-saving local times are skipped. This keeps
    a recurring reminder from being sent twice or at an unintended local time.
    """

    zone = ZoneInfo(validate_timezone(timezone_name))
    reference_utc = _as_utc(reference)
    local_reference = reference_utc.astimezone(zone)
    candidate_date = local_reference.date()

    if recurrence_kind == "daily":
        return _first_valid_after(
            candidate_dates=(candidate_date + timedelta(days=offset) for offset in range(3_660)),
            time_text=time_text,
            timezone_name=timezone_name,
            reference=reference_utc,
        )
    if recurrence_kind == "weekly":
        if not weekdays:
            raise InvalidScheduleError("每週提醒至少要指定一天")
        return _first_valid_after(
            candidate_dates=(
                candidate_date + timedelta(days=offset)
                for offset in range(3_660)
                if (candidate_date + timedelta(days=offset)).weekday() in weekdays
            ),
            time_text=time_text,
            timezone_name=timezone_name,
            reference=reference_utc,
        )
    if recurrence_kind == "interval":
        if interval_days is None or interval_days <= 0 or start_date is None:
            raise InvalidScheduleError("固定間隔提醒缺少有效的起始日期或天數")
        if candidate_date > start_date:
            elapsed_days = (candidate_date - start_date).days
            offset = max(elapsed_days // interval_days - 1, 0)
        else:
            offset = 0
        return _first_valid_after(
            candidate_dates=(
                start_date + timedelta(days=interval_days * (offset + index))
                for index in range(3_660)
            ),
            time_text=time_text,
            timezone_name=timezone_name,
            reference=reference_utc,
        )
    raise InvalidScheduleError("不支援的重複提醒類型")


def _first_valid_after(
    *,
    candidate_dates: Iterable[date],
    time_text: str,
    timezone_name: str,
    reference: datetime,
) -> datetime:
    for candidate_date in candidate_dates:
        try:
            candidate = parse_local_datetime(
                date_text=candidate_date.isoformat(),
                time_text=time_text,
                timezone_name=timezone_name,
            )
        except InvalidScheduleError:
            continue
        if candidate > reference:
            return candidate
    raise InvalidScheduleError("找不到下一個有效的提醒時間")


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
