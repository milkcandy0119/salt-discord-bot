from datetime import UTC, datetime, timedelta

import pytest

from app.bot.reminder_commands import ReminderCommandGroup, TimezoneCommandGroup
from app.reminders.dispatcher import (
    ReminderDeliveryError,
    ReminderDispatcher,
)
from app.reminders.service import (
    InvalidReminderError,
    ReminderService,
    SensitiveReminderError,
)
from app.security.sensitive_filter import SensitiveFilter
from app.storage.database import Database
from app.storage.reminders import Reminder, ReminderRepository


class FakeReminderSender:
    """不接觸 Discord 的提醒發送替身。"""

    def __init__(self, *, error: ReminderDeliveryError | None = None) -> None:
        self.error = error
        self.sent: list[Reminder] = []

    async def send(self, reminder: Reminder) -> None:
        if self.error is not None:
            raise self.error
        self.sent.append(reminder)


def _service(database: Database) -> tuple[ReminderRepository, ReminderService]:
    repository = ReminderRepository(database.session_factory)
    return repository, ReminderService(
        repository,
        sensitive_filter=SensitiveFilter(),
        default_timezone="Asia/Taipei",
        max_attempts=5,
    )


def test_reminder_slash_commands_only_target_current_interaction_user(
    database: Database,
) -> None:
    _, service = _service(database)
    reminder_group = ReminderCommandGroup(
        service=service,
        allowed_guild_ids=frozenset({1}),
    )
    timezone_group = TimezoneCommandGroup(
        service=service,
        allowed_guild_ids=frozenset({1}),
    )

    reminder_commands = {command.name: command for command in reminder_group.commands}
    timezone_commands = {command.name: command for command in timezone_group.commands}
    assert set(reminder_commands) == {"create", "list", "cancel"}
    assert set(timezone_commands) == {"view", "set"}
    assert all(
        "user" not in {parameter.name for parameter in command.parameters}
        for command in (*reminder_group.commands, *timezone_group.commands)
    )


@pytest.mark.asyncio
async def test_reminder_uses_user_timezone_and_is_owner_scoped(database: Database) -> None:
    repository, service = _service(database)
    now = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    assert await service.get_timezone(guild_id="1", user_id="10") == "Asia/Taipei"
    await service.set_timezone(
        guild_id="1",
        user_id="10",
        timezone_name="Asia/Tokyo",
    )

    reminder = await service.create(
        guild_id="1",
        user_id="10",
        date_text="2026-08-05",
        time_text="09:30",
        content="帶雨傘",
        now=now,
    )

    assert reminder.due_at == datetime(2026, 8, 5, 0, 30, tzinfo=UTC)
    assert "Asia/Tokyo" in service.format_due_at(reminder)
    assert len(await service.list_own(guild_id="1", user_id="10")) == 1
    assert await service.list_own(guild_id="1", user_id="11") == ()
    assert not await service.cancel(
        guild_id="1",
        user_id="11",
        reminder_id=reminder.id,
    )
    assert await service.cancel(
        guild_id="1",
        user_id="10",
        reminder_id=reminder.id,
    )
    assert await repository.status_counts() == {"cancelled": 1}


@pytest.mark.asyncio
async def test_reminder_rejects_past_invalid_and_sensitive_inputs(database: Database) -> None:
    _, service = _service(database)
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    await service.set_timezone(guild_id="1", user_id="10", timezone_name="Asia/Taipei")

    with pytest.raises(InvalidReminderError, match="晚於目前時間"):
        await service.create(
            guild_id="1",
            user_id="10",
            date_text="2026-08-04",
            time_text="19:00",
            content="過去的提醒",
            now=now,
        )
    with pytest.raises(InvalidReminderError, match="YYYY-MM-DD"):
        await service.create(
            guild_id="1",
            user_id="10",
            date_text="明天",
            time_text="晚上",
            content="模糊提醒",
            now=now,
        )
    with pytest.raises(SensitiveReminderError):
        await service.create(
            guild_id="1",
            user_id="10",
            date_text="2026-08-05",
            time_text="19:00",
            content="sk-proj-abcdefghijklmnopqrstuvwxyz123456",
            now=now,
        )


def test_dst_nonexistent_and_ambiguous_times_are_rejected() -> None:
    with pytest.raises(InvalidReminderError, match="不存在"):
        ReminderService.parse_local_datetime(
            date_text="2026-03-08",
            time_text="02:30",
            timezone_name="America/New_York",
        )
    with pytest.raises(InvalidReminderError, match="重複"):
        ReminderService.parse_local_datetime(
            date_text="2026-11-01",
            time_text="01:30",
            timezone_name="America/New_York",
        )


@pytest.mark.asyncio
async def test_dispatcher_sends_due_reminder_and_marks_it_sent(database: Database) -> None:
    repository = ReminderRepository(database.session_factory)
    now = datetime.now(UTC)
    reminder = await repository.create(
        guild_id="1",
        user_id="10",
        content="喝水",
        timezone_name="Asia/Taipei",
        due_at=now - timedelta(minutes=1),
        max_attempts=5,
        now=now - timedelta(minutes=2),
    )
    sender = FakeReminderSender()
    dispatcher = ReminderDispatcher(
        repository=repository,
        sender=sender,
        stale_after=timedelta(minutes=5),
        retry_base_delay=timedelta(minutes=1),
        maximum_per_run=20,
    )

    result = await dispatcher.run_once()

    assert result.sent == 1
    assert sender.sent[0].id == reminder.id
    assert await repository.status_counts() == {"sent": 1}


@pytest.mark.asyncio
async def test_dm_unavailable_keeps_failed_reminder_without_public_fallback(
    database: Database,
) -> None:
    repository = ReminderRepository(database.session_factory)
    now = datetime.now(UTC)
    await repository.create(
        guild_id="1",
        user_id="10",
        content="只有私訊",
        timezone_name="Asia/Taipei",
        due_at=now - timedelta(minutes=1),
        max_attempts=5,
        now=now - timedelta(minutes=2),
    )
    sender = FakeReminderSender(
        error=ReminderDeliveryError("discord_dm_unavailable", retryable=False)
    )
    dispatcher = ReminderDispatcher(
        repository=repository,
        sender=sender,
        stale_after=timedelta(minutes=5),
        retry_base_delay=timedelta(minutes=1),
        maximum_per_run=20,
    )

    result = await dispatcher.run_once()

    assert result.failed == 1
    assert sender.sent == []
    assert await repository.status_counts() == {"failed": 1}


@pytest.mark.asyncio
async def test_stale_sending_reminder_is_recovered_after_restart(database: Database) -> None:
    repository = ReminderRepository(database.session_factory)
    base = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    created = await repository.create(
        guild_id="1",
        user_id="10",
        content="重啟恢復",
        timezone_name="Asia/Taipei",
        due_at=base,
        max_attempts=5,
        now=base - timedelta(minutes=1),
    )
    first = await repository.claim_due(stale_after=timedelta(minutes=5), now=base)
    restarted = ReminderRepository(database.session_factory)
    recovered = await restarted.claim_due(
        stale_after=timedelta(minutes=5),
        now=base + timedelta(minutes=6),
    )

    assert first is not None
    assert recovered is not None
    assert recovered.id == created.id
    assert recovered.attempts == 2
