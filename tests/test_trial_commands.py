from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.bot.trial_commands import TrialCommandGroup
from app.storage.admin_audit import AdminAuditRepository
from app.storage.database import Database
from app.storage.models import MessageRecord
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


@pytest.mark.asyncio
async def test_trial_feedback_is_private_admin_only_and_content_free(
    database: Database,
) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    repository = TrialRepository(database.session_factory)
    audit = AdminAuditRepository(database.session_factory)
    await repository.start(
        guild_ids=frozenset({1}),
        channel_ids=frozenset({10}),
        companion_channel_ids=frozenset({10}),
        timezone_name="Asia/Taipei",
        duration=timedelta(days=7),
        global_increment_limit_microusd=1_000_000,
        background_increment_limit_microusd=250_000,
        companion_daily_reply_limit=20,
        now=now,
    )
    async with database.session_factory() as session, session.begin():
        session.add(
            MessageRecord(
                discord_message_id="123456789",
                guild_id="1",
                channel_id="10",
                author_id="20",
                author_display_name="不應出現在評價",
                content="不應複製到試跑評價的聊天內容",
                discord_created_at=now + timedelta(minutes=1),
                received_at=now + timedelta(minutes=1),
                replied_to_message_id=None,
                is_bot=False,
                is_sensitive=False,
                sensitive_categories=[],
                processing_status="stored",
                author_notification_status="not_required",
                admin_notification_status="not_required",
            )
        )
    group = TrialCommandGroup(
        repository=repository,
        audit_repository=audit,
        allowed_guild_ids=frozenset({1}),
        admin_user_ids=frozenset({9}),
    )
    command = next(command for command in group.commands if command.name == "feedback")

    denied = FakeInteraction(guild_id=1, user_id=8)
    await command.callback(  # type: ignore[misc]
        group,
        denied,  # type: ignore[arg-type]
        "123456789",
        "too_formal",
    )
    assert denied.response.content == "你沒有提交試跑評價的權限"
    assert denied.response.ephemeral is True
    assert await audit.count() == 0

    allowed = FakeInteraction(guild_id=1, user_id=9)
    await command.callback(  # type: ignore[misc]
        group,
        allowed,  # type: ignore[arg-type]
        "123456789",
        "too_formal",
    )
    assert allowed.response.content == "試跑評價已記錄"
    assert allowed.response.ephemeral is True
    assert await audit.count(action="trial_feedback") == 1
    report = await repository.report(now=now + timedelta(minutes=2))
    assert report["feedback_counts"] == {"too_formal": 1}
    assert "不應複製" not in str(report)
