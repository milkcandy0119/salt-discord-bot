from datetime import UTC, datetime, timedelta

import pytest

from app.reminders.pending_service import PendingReminderService
from app.reminders.service import ReminderService
from app.security.sensitive_filter import SensitiveFilter
from app.storage.database import Database
from app.storage.pending_actions import PendingActionRepository
from app.storage.reminders import ReminderRepository


@pytest.mark.asyncio
async def test_pending_action_is_scoped_expiring_and_single_use(database: Database) -> None:
    repository = PendingActionRepository(database.session_factory)
    created = await repository.create(
        guild_id="1", channel_id="2", user_id="3", action_type="create_reminder",
        parsed_parameters={"date": "2026-08-06"}, now=datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert await repository.claim_for_execution(
        action_id=created.id, guild_id="1", channel_id="2", user_id="4"
    ) is None
    claimed = await repository.claim_for_execution(
        action_id=created.id, guild_id="1", channel_id="2", user_id="3",
        now=datetime(2026, 8, 5, 0, 1, tzinfo=UTC),
    )
    assert claimed is not None
    assert await repository.claim_for_execution(
        action_id=created.id, guild_id="1", channel_id="2", user_id="3"
    ) is None
    expired = await repository.create(
        guild_id="1", channel_id="2", user_id="3", action_type="create_reminder",
        parsed_parameters={}, expires_after=timedelta(seconds=1),
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )
    assert await repository.claim_for_execution(
        action_id=expired.id, guild_id="1", channel_id="2", user_id="3",
        now=datetime(2026, 8, 5, 0, 1, 1, tzinfo=UTC),
    ) is None


@pytest.mark.asyncio
async def test_confirmation_creates_one_reminder_only_after_explicit_confirm(
    database: Database,
) -> None:
    reminders = ReminderRepository(database.session_factory)
    reminder_service = ReminderService(reminders, sensitive_filter=SensitiveFilter())
    await reminder_service.set_timezone(guild_id="1", user_id="3", timezone_name="Asia/Taipei")
    service = PendingReminderService(
        pending_repository=PendingActionRepository(database.session_factory),
        reminder_service=reminder_service,
    )
    action, parsed = await service.propose(
        guild_id="1", channel_id="2", user_id="3", text="明天晚上 8 點提醒我交報告"
    )
    assert parsed.content == "交報告"
    assert await reminders.list_own_pending(guild_id="1", user_id="3") == ()
    created = await service.confirm(
        action_id=action.id, guild_id="1", channel_id="2", user_id="3"
    )
    assert created is not None
    assert await service.confirm(
        action_id=action.id, guild_id="1", channel_id="2", user_id="3"
    ) is None
    assert len(await reminders.list_own_pending(guild_id="1", user_id="3")) == 1
