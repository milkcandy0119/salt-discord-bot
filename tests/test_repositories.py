import asyncio
from datetime import UTC, datetime

import pytest

from app.storage.database import Database
from app.storage.repositories import MessageRepository, NewMessage


def make_message(*, discord_message_id: str = "100") -> NewMessage:
    return NewMessage(
        discord_message_id=discord_message_id,
        guild_id="1",
        channel_id="2",
        author_id="3",
        author_display_name="測試者",
        content="測試訊息",
        discord_created_at=datetime(2026, 8, 3, tzinfo=UTC),
        received_at=datetime(2026, 8, 3, tzinfo=UTC),
        replied_to_message_id="99",
        is_bot=False,
        is_sensitive=False,
        sensitive_categories=(),
    )


@pytest.mark.asyncio
async def test_duplicate_discord_event_is_idempotent(message_repository: MessageRepository) -> None:
    first = await message_repository.save(make_message())
    second = await message_repository.save(make_message())

    assert first.created is True
    assert second.created is False
    assert first.message.discord_message_id == second.message.discord_message_id
    assert await message_repository.count() == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_events_create_one_record(
    message_repository: MessageRepository,
) -> None:
    results = await asyncio.gather(
        message_repository.save(make_message(discord_message_id="concurrent")),
        message_repository.save(make_message(discord_message_id="concurrent")),
    )

    assert sum(result.created for result in results) == 1
    assert await message_repository.count() == 1


@pytest.mark.asyncio
async def test_message_survives_database_reopen(migrated_database_url: str) -> None:
    first_database = Database(migrated_database_url)
    first_repository = MessageRepository(first_database.session_factory)
    await first_repository.save(make_message(discord_message_id="restart-test"))
    await first_database.dispose()

    reopened_database = Database(migrated_database_url)
    reopened_repository = MessageRepository(reopened_database.session_factory)
    stored = await reopened_repository.get_by_discord_id("restart-test")
    await reopened_database.dispose()

    assert stored is not None
    assert stored.content == "測試訊息"
    assert stored.replied_to_message_id == "99"
