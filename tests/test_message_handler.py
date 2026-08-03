from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

import pytest

from app.bot.message_handler import (
    IncomingMessage,
    MessageHandler,
    SensitiveNotice,
)
from app.security.sensitive_filter import SensitiveFilter
from app.storage.repositories import MessageRepository


@dataclass
class FakeNotifier:
    author_notices: list[SensitiveNotice] = field(default_factory=list)
    admin_notices: list[SensitiveNotice] = field(default_factory=list)

    async def notify_author(self, notice: SensitiveNotice) -> None:
        self.author_notices.append(notice)

    async def notify_admins(self, notice: SensitiveNotice) -> None:
        self.admin_notices.append(notice)


class FailingNotifier(FakeNotifier):
    async def notify_author(self, notice: SensitiveNotice) -> None:
        raise RuntimeError("模擬作者私訊失敗")

    async def notify_admins(self, notice: SensitiveNotice) -> None:
        raise RuntimeError("模擬管理員通知失敗")


@dataclass
class FakeSegmenter:
    assigned_message_ids: list[str] = field(default_factory=list)

    async def assign_message(self, discord_message_id: str) -> None:
        self.assigned_message_ids.append(discord_message_id)


def make_incoming_message(**overrides: object) -> IncomingMessage:
    values: dict[str, object] = {
        "discord_message_id": "100",
        "guild_id": 1,
        "channel_id": 2,
        "author_id": 3,
        "author_display_name": "測試者",
        "content": "一般訊息",
        "discord_created_at": datetime(2026, 8, 3, tzinfo=UTC),
        "replied_to_message_id": None,
        "author_is_bot": False,
        "is_own_message": False,
    }
    values.update(overrides)
    return IncomingMessage(
        discord_message_id=cast(str, values["discord_message_id"]),
        guild_id=cast(int | None, values["guild_id"]),
        channel_id=cast(int, values["channel_id"]),
        author_id=cast(int, values["author_id"]),
        author_display_name=cast(str | None, values["author_display_name"]),
        content=cast(str, values["content"]),
        discord_created_at=cast(datetime, values["discord_created_at"]),
        replied_to_message_id=cast(str | None, values["replied_to_message_id"]),
        author_is_bot=cast(bool, values["author_is_bot"]),
        is_own_message=cast(bool, values["is_own_message"]),
    )


def make_handler(
    repository: MessageRepository,
    notifier: FakeNotifier,
    segmenter: FakeSegmenter | None = None,
) -> MessageHandler:
    return MessageHandler(
        repository=repository,
        sensitive_filter=SensitiveFilter(),
        notifier=notifier,
        segmenter=segmenter or FakeSegmenter(),
        allowed_guild_ids=frozenset({1}),
        allowed_channel_ids=frozenset({2}),
    )


@pytest.mark.asyncio
async def test_non_whitelisted_message_is_not_saved(
    message_repository: MessageRepository,
) -> None:
    notifier = FakeNotifier()
    handler = make_handler(message_repository, notifier)

    outcome = await handler.handle(make_incoming_message(channel_id=999))

    assert outcome.status == "ignored_not_allowed"
    assert await message_repository.count() == 0


@pytest.mark.asyncio
async def test_own_bot_message_is_ignored(message_repository: MessageRepository) -> None:
    notifier = FakeNotifier()
    handler = make_handler(message_repository, notifier)

    outcome = await handler.handle(make_incoming_message(is_own_message=True, author_is_bot=True))

    assert outcome.status == "ignored_own_message"
    assert await message_repository.count() == 0


@pytest.mark.asyncio
async def test_reply_id_is_saved_and_duplicate_event_is_not_repeated(
    message_repository: MessageRepository,
) -> None:
    notifier = FakeNotifier()
    handler = make_handler(message_repository, notifier)
    message = make_incoming_message(replied_to_message_id="88")

    first = await handler.handle(message)
    second = await handler.handle(message)
    stored = await message_repository.get_by_discord_id("100")

    assert first.status == "stored"
    assert second.status == "duplicate"
    assert stored is not None
    assert stored.replied_to_message_id == "88"
    assert await message_repository.count() == 1


@pytest.mark.asyncio
async def test_sensitive_content_is_masked_before_storage_and_notifications(
    message_repository: MessageRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    notifier = FakeNotifier()
    handler = make_handler(message_repository, notifier)
    secret = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz123456"

    outcome = await handler.handle(make_incoming_message(content=f"key={secret}"))
    stored = await message_repository.get_by_discord_id("100")

    assert outcome.status == "stored_sensitive"
    assert stored is not None
    assert stored.is_sensitive is True
    assert secret not in stored.content
    assert stored.sensitive_categories == ["openai_api_key"]
    assert stored.author_notification_status == "sent"
    assert stored.admin_notification_status == "sent"
    assert len(notifier.author_notices) == 1
    assert len(notifier.admin_notices) == 1
    assert not hasattr(notifier.author_notices[0], "content")
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_notification_failure_does_not_remove_saved_sensitive_message(
    message_repository: MessageRepository,
) -> None:
    handler = MessageHandler(
        repository=message_repository,
        sensitive_filter=SensitiveFilter(),
        notifier=FailingNotifier(),
        segmenter=FakeSegmenter(),
        allowed_guild_ids=frozenset({1}),
        allowed_channel_ids=frozenset({2}),
    )
    secret = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz123456"

    outcome = await handler.handle(make_incoming_message(content=secret))
    stored = await message_repository.get_by_discord_id("100")

    assert outcome.status == "stored_sensitive"
    assert stored is not None
    assert secret not in stored.content
    assert stored.author_notification_status == "failed"
    assert stored.admin_notification_status == "failed"


@pytest.mark.asyncio
async def test_sensitive_display_name_is_masked_before_storage(
    message_repository: MessageRepository,
) -> None:
    notifier = FakeNotifier()
    handler = make_handler(message_repository, notifier)
    secret = "display-name-secret"

    await handler.handle(make_incoming_message(author_display_name=f"token={secret}"))
    stored = await message_repository.get_by_discord_id("100")

    assert stored is not None
    assert stored.is_sensitive is True
    assert stored.author_display_name is not None
    assert secret not in stored.author_display_name


@pytest.mark.asyncio
async def test_new_message_is_segmented_once_after_it_is_saved(
    message_repository: MessageRepository,
) -> None:
    notifier = FakeNotifier()
    segmenter = FakeSegmenter()
    handler = make_handler(message_repository, notifier, segmenter)
    message = make_incoming_message()

    await handler.handle(message)
    await handler.handle(message)

    assert segmenter.assigned_message_ids == ["100"]
