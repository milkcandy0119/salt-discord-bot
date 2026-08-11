from datetime import UTC, datetime, timedelta

import discord
import pytest

from app.bot.reminder_commands import (
    ReminderCommandGroup,
    TimezoneCommandGroup,
    _BulkReminderView,
    _CreateReminderView,
)
from app.reminders.dispatcher import ReminderDeliveryError, ReminderDispatcher
from app.reminders.service import (
    InvalidReminderError,
    ReminderService,
    SensitiveReminderError,
)
from app.security.sensitive_filter import SensitiveFilter
from app.storage.database import Database
from app.storage.reminders import Reminder, ReminderRepository


class FakeReminderSender:
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


def test_reminder_slash_commands_expose_creation_modes(database: Database) -> None:
    _, service = _service(database)
    reminder_group = ReminderCommandGroup(service=service, allowed_guild_ids=frozenset({1}))
    timezone_group = TimezoneCommandGroup(service=service, allowed_guild_ids=frozenset({1}))

    reminder_commands = {command.name: command for command in reminder_group.commands}
    timezone_commands = {command.name: command for command in timezone_group.commands}

    assert set(reminder_commands) == {"create", "manage", "list", "cancel"}
    assert set(timezone_commands) == {"view", "set"}
    assert reminder_commands["create"].parameters == []
    create_view = _CreateReminderView(service=service, guild_id="1", user_id="10")
    assert create_view.children[0].options[0].value == "once"
    assert {option.value for option in create_view.children[0].options} == {
        "once",
        "daily",
        "weekly",
        "interval",
    }


@pytest.mark.asyncio
async def test_one_time_reminder_uses_the_owner_timezone(database: Database) -> None:
    repository, service = _service(database)
    now = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    await service.set_timezone(guild_id="1", user_id="10", timezone_name="Asia/Tokyo")

    reminder = await service.create(
        guild_id="1",
        user_id="10",
        date_text="2026-08-05",
        time_text="09:30",
        content="submit report",
        now=now,
    )

    assert reminder.due_at == datetime(2026, 8, 5, 0, 30, tzinfo=UTC)
    assert reminder.recurrence_kind == "once"
    assert len(await service.list_own(guild_id="1", user_id="10")) == 1
    assert not await service.cancel(guild_id="1", user_id="11", reminder_id=reminder.id)
    assert await service.cancel(guild_id="1", user_id="10", reminder_id=reminder.id)
    assert await repository.status_counts() == {"cancelled": 1}


@pytest.mark.asyncio
async def test_reminder_rejects_past_invalid_and_sensitive_inputs(database: Database) -> None:
    _, service = _service(database)
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    await service.set_timezone(guild_id="1", user_id="10", timezone_name="Asia/Taipei")

    with pytest.raises(InvalidReminderError):
        await service.create(
            guild_id="1",
            user_id="10",
            date_text="2026-08-04",
            time_text="19:00",
            content="past reminder",
            now=now,
        )
    with pytest.raises(InvalidReminderError, match="YYYY-MM-DD"):
        await service.create(
            guild_id="1",
            user_id="10",
            date_text="invalid",
            time_text="invalid",
            content="bad date",
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


@pytest.mark.asyncio
async def test_recurring_reminders_calculate_their_first_local_occurrence(
    database: Database,
) -> None:
    _, service = _service(database)
    now = datetime(2026, 8, 4, 0, 30, tzinfo=UTC)
    await service.set_timezone(guild_id="1", user_id="10", timezone_name="Asia/Taipei")

    daily = await service.create_daily(
        guild_id="1", user_id="10", time_text="09:00", content="daily", now=now
    )
    weekly = await service.create_weekly(
        guild_id="1",
        user_id="10",
        weekdays_text="wed,fri",
        time_text="09:00",
        content="weekly",
        now=now,
    )
    interval = await service.create_interval(
        guild_id="1",
        user_id="10",
        every_text="3d",
        start_date_text="2026-08-01",
        time_text="09:00",
        content="interval",
        now=now,
    )

    assert daily.due_at == datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
    assert weekly.due_at == datetime(2026, 8, 5, 1, 0, tzinfo=UTC)
    assert weekly.recurrence_weekdays == (2, 4)
    assert interval.due_at == datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
    assert interval.interval_days == 3


@pytest.mark.asyncio
async def test_bulk_content_update_and_cancellation_are_owner_scoped(
    database: Database,
) -> None:
    repository, service = _service(database)
    now = datetime(2026, 8, 4, 0, 0, tzinfo=UTC)
    await service.set_timezone(guild_id="1", user_id="10", timezone_name="Asia/Taipei")
    first = await service.create(
        guild_id="1",
        user_id="10",
        date_text="2026-08-05",
        time_text="09:00",
        content="first",
        now=now,
    )
    second = await service.create(
        guild_id="1",
        user_id="10",
        date_text="2026-08-06",
        time_text="09:00",
        content="second",
        now=now,
    )

    updated = await service.update_many_content(
        guild_id="1",
        user_id="10",
        reminder_ids=(first.id, second.id, first.id),
        content="updated",
    )
    reminders = await service.list_own(guild_id="1", user_id="10")
    view = _BulkReminderView(
        service=service,
        guild_id="1",
        user_id="10",
        reminders=reminders,
    )
    cancelled_by_other_user = await service.cancel_many(
        guild_id="1",
        user_id="11",
        reminder_ids=(first.id, second.id),
    )
    cancelled = await service.cancel_many(
        guild_id="1",
        user_id="10",
        reminder_ids=(first.id, second.id),
    )

    assert updated == 2
    assert {reminder.content for reminder in reminders} == {"updated"}
    select = next(
        child for child in view.children if child.type is discord.ComponentType.select
    )
    assert len(select.options) == 2
    assert cancelled_by_other_user == 0
    assert cancelled == 2
    assert await repository.status_counts() == {"cancelled": 2}


def test_dst_nonexistent_and_ambiguous_times_are_rejected() -> None:
    with pytest.raises(InvalidReminderError):
        ReminderService.parse_local_datetime(
            date_text="2026-03-08",
            time_text="02:30",
            timezone_name="America/New_York",
        )
    with pytest.raises(InvalidReminderError):
        ReminderService.parse_local_datetime(
            date_text="2026-11-01",
            time_text="01:30",
            timezone_name="America/New_York",
        )


@pytest.mark.asyncio
async def test_dispatcher_marks_sent_one_time_reminder(database: Database) -> None:
    repository = ReminderRepository(database.session_factory)
    now = datetime.now(UTC)
    reminder = await repository.create(
        guild_id="1",
        user_id="10",
        content="single",
        timezone_name="Asia/Taipei",
        due_at=now - timedelta(minutes=1),
        max_attempts=5,
        now=now - timedelta(minutes=2),
    )
    sender = FakeReminderSender()
    dispatcher = _dispatcher(repository, sender)

    result = await dispatcher.run_once()

    assert result.sent == 1
    assert sender.sent[0].id == reminder.id
    assert await repository.status_counts() == {"sent": 1}


@pytest.mark.asyncio
async def test_dispatcher_advances_successful_daily_reminder(database: Database) -> None:
    repository = ReminderRepository(database.session_factory)
    now = datetime.now(UTC)
    reminder = await repository.create(
        guild_id="1",
        user_id="10",
        content="daily",
        timezone_name="Asia/Taipei",
        due_at=now - timedelta(minutes=1),
        max_attempts=5,
        recurrence_kind="daily",
        recurrence_time="09:00",
        now=now - timedelta(minutes=2),
    )
    sender = FakeReminderSender()

    result = await _dispatcher(repository, sender).run_once()
    scheduled = await repository.list_own_pending(guild_id="1", user_id="10")

    assert result.sent == 1
    assert sender.sent[0].id == reminder.id
    assert len(scheduled) == 1
    assert scheduled[0].status == "pending"
    assert scheduled[0].attempts == 0
    assert scheduled[0].due_at.replace(tzinfo=UTC) > now


@pytest.mark.asyncio
async def test_non_retryable_delivery_failure_marks_one_time_reminder_failed(
    database: Database,
) -> None:
    repository = ReminderRepository(database.session_factory)
    now = datetime.now(UTC)
    await repository.create(
        guild_id="1",
        user_id="10",
        content="unavailable dm",
        timezone_name="Asia/Taipei",
        due_at=now - timedelta(minutes=1),
        max_attempts=5,
        now=now - timedelta(minutes=2),
    )
    sender = FakeReminderSender(
        error=ReminderDeliveryError("discord_dm_unavailable", retryable=False)
    )

    result = await _dispatcher(repository, sender).run_once()

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
        content="restart",
        timezone_name="Asia/Taipei",
        due_at=base,
        max_attempts=5,
        now=base - timedelta(minutes=1),
    )
    first = await repository.claim_due(stale_after=timedelta(minutes=5), now=base)
    restarted = ReminderRepository(database.session_factory)
    recovered = await restarted.claim_due(
        stale_after=timedelta(minutes=5), now=base + timedelta(minutes=6)
    )

    assert first is not None
    assert recovered is not None
    assert recovered.id == created.id
    assert recovered.attempts == 2


def _dispatcher(repository: ReminderRepository, sender: FakeReminderSender) -> ReminderDispatcher:
    return ReminderDispatcher(
        repository=repository,
        sender=sender,
        stale_after=timedelta(minutes=5),
        retry_base_delay=timedelta(minutes=1),
        maximum_per_run=20,
    )
