from datetime import UTC, datetime

import discord
import pytest

from app.bot.reminder_sender import DiscordReminderSender
from app.storage.reminders import Reminder


class FakeReminderUser:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> None:
        self.sent.append(kwargs)


class FakeReminderClient:
    def __init__(self, user: FakeReminderUser) -> None:
        self._user = user

    def get_user(self, user_id: int) -> FakeReminderUser | None:
        return self._user if user_id == 10 else None

    async def fetch_user(self, user_id: int) -> FakeReminderUser:
        assert user_id == 10
        return self._user


@pytest.mark.asyncio
async def test_discord_reminder_sender_sends_a_private_embed() -> None:
    user = FakeReminderUser()
    sender = DiscordReminderSender(FakeReminderClient(user))  # type: ignore[arg-type]
    reminder = Reminder(
        id=7,
        guild_id="1",
        user_id="10",
        content="submit report",
        timezone_name="Asia/Taipei",
        recurrence_kind="daily",
        recurrence_time="09:00",
        recurrence_weekdays=(),
        interval_days=None,
        recurrence_start_date=None,
        due_at=datetime(2026, 8, 11, 1, 0, tzinfo=UTC),
        status="sending",
        attempts=1,
        max_attempts=5,
        last_error_code=None,
    )

    await sender.send(reminder)

    assert len(user.sent) == 1
    embed = user.sent[0]["embed"]
    assert isinstance(embed, discord.Embed)
    assert embed.title == "⏰ 提醒時間到了"
    assert embed.description == "submit report"
    assert [(field.name, field.value) for field in embed.fields] == [
        ("提醒規則", "每天"),
        ("設定時間", "2026-08-11 09:00 Asia/Taipei"),
    ]
    mentions = user.sent[0]["allowed_mentions"]
    assert isinstance(mentions, discord.AllowedMentions)
    assert not mentions.everyone
    assert not mentions.users
    assert not mentions.roles
