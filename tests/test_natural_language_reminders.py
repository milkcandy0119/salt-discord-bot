from datetime import UTC, datetime

import pytest

from app.reminders.natural_language import NaturalLanguageReminderParser
from app.reminders.service import InvalidReminderError


@pytest.mark.parametrize(
    ("text", "date_text", "time_text", "content"),
    (
        ("明天早上 9 點提醒我開會", "2026-08-06", "09:00", "開會"),
        ("明天晚上 8 點交報告", "2026-08-06", "20:00", "交報告"),
        ("30 分鐘後提醒我喝水", "2026-08-05", "12:30", "喝水"),
        ("下週一下午 3 點提醒我繳費", "2026-08-10", "15:00", "繳費"),
    ),
)
def test_parser_understands_explicit_chinese_times(
    text: str, date_text: str, time_text: str, content: str
) -> None:
    result = NaturalLanguageReminderParser().parse(
        text=text,
        timezone_name="Asia/Taipei",
        now=datetime(2026, 8, 5, 4, 0, tzinfo=UTC),
    )
    assert (result.date_text, result.time_text, result.content) == (date_text, time_text, content)


@pytest.mark.parametrize("text", ("晚點提醒我喝水", "明天早上提醒我開會", "明天 9 點"))
def test_parser_rejects_ambiguous_or_contentless_requests(text: str) -> None:
    with pytest.raises(InvalidReminderError):
        NaturalLanguageReminderParser().parse(
            text=text,
            timezone_name="Asia/Taipei",
            now=datetime(2026, 8, 5, 4, 0, tzinfo=UTC),
        )
