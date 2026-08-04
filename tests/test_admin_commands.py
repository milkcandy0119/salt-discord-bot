from dataclasses import dataclass

import pytest

from app.ai.budget_manager import BudgetManager
from app.bot.admin_commands import BotAdminCommandGroup
from app.storage.admin_audit import AdminAuditRepository
from app.storage.background_memory import BackgroundMemoryRepository
from app.storage.database import Database
from app.storage.reminders import ReminderRepository
from app.storage.trial import TrialRepository


@dataclass
class FakeUser:
    id: int


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.content: str | None = None
        self.ephemeral: bool | None = None

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.content = content
        self.ephemeral = bool(kwargs.get("ephemeral"))


class FakeInteraction:
    def __init__(self, *, guild_id: int | None, user_id: int) -> None:
        self.guild_id = guild_id
        self.user = FakeUser(user_id)
        self.response = FakeInteractionResponse()


class FakeClient:
    latency = 0.125

    @staticmethod
    def is_ready() -> bool:
        return True


@pytest.mark.asyncio
async def test_bot_status_is_private_and_restricted_to_configured_admins(
    database: Database,
) -> None:
    audit = AdminAuditRepository(database.session_factory)
    group = BotAdminCommandGroup(
        client=FakeClient(),  # type: ignore[arg-type]
        budget_manager=BudgetManager(database.session_factory),
        background_repository=BackgroundMemoryRepository(database.session_factory),
        reminder_repository=ReminderRepository(database.session_factory),
        audit_repository=audit,
        allowed_guild_ids=frozenset({1}),
        admin_user_ids=frozenset({9}),
        trial_repository=TrialRepository(database.session_factory),
    )
    command = next(command for command in group.commands if command.name == "status")

    denied = FakeInteraction(guild_id=1, user_id=8)
    await command.callback(group, denied)  # type: ignore[misc, arg-type]
    assert denied.response.ephemeral is True
    assert denied.response.content == "你沒有查看機器人管理狀態的權限"
    assert await audit.count() == 0

    allowed = FakeInteraction(guild_id=1, user_id=9)
    await command.callback(group, allowed)  # type: ignore[misc, arg-type]
    assert allowed.response.ephemeral is True
    assert allowed.response.content is not None
    assert "連線：ready" in allowed.response.content
    assert "Discord 延遲：約 125 ms" in allowed.response.content
    assert "提醒：{}" in allowed.response.content
    assert await audit.count(action="bot_status_view") == 1

    trial_command = next(
        command for command in group.commands if command.name == "trial-status"
    )
    denied_trial = FakeInteraction(guild_id=1, user_id=8)
    await trial_command.callback(group, denied_trial)  # type: ignore[misc, arg-type]
    assert denied_trial.response.content == "你沒有查看試跑狀態的權限"
    assert await audit.count(action="trial_status_view") == 0

    allowed_trial = FakeInteraction(guild_id=1, user_id=9)
    await trial_command.callback(group, allowed_trial)  # type: ignore[misc, arg-type]
    assert allowed_trial.response.content == "階段 9 試跑尚未開始"
    assert allowed_trial.response.ephemeral is True
    assert await audit.count(action="trial_status_view") == 1
