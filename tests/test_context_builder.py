from datetime import UTC, datetime, timedelta

import pytest

from app.conversations.context_builder import ContextBuilder
from app.conversations.segmenter import ConversationSegmenter
from app.storage.database import Database
from app.storage.repositories import MessageRepository, NewMessage

BASE_TIME = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


async def save_message(
    repository: MessageRepository,
    segmenter: ConversationSegmenter,
    *,
    message_id: str,
    author_id: str,
    content: str,
    minute: int,
    replied_to: str | None = None,
    is_bot: bool = False,
    is_sensitive: bool = False,
) -> None:
    await repository.save(
        NewMessage(
            discord_message_id=message_id,
            guild_id="1",
            channel_id="2",
            author_id=author_id,
            author_display_name="助手" if is_bot else f"使用者 {author_id}",
            content=content,
            discord_created_at=BASE_TIME + timedelta(minutes=minute),
            received_at=BASE_TIME + timedelta(minutes=minute),
            replied_to_message_id=replied_to,
            is_bot=is_bot,
            is_sensitive=is_sensitive,
            sensitive_categories=("generic_secret",) if is_sensitive else (),
        )
    )
    await segmenter.assign_message(message_id)


@pytest.mark.asyncio
async def test_context_prioritizes_reply_chain_and_excludes_sensitive_messages(
    database: Database,
) -> None:
    repository = MessageRepository(database.session_factory)
    segmenter = ConversationSegmenter(database.session_factory)
    await save_message(
        repository,
        segmenter,
        message_id="root",
        author_id="1",
        content="最初的重要問題",
        minute=0,
    )
    await save_message(
        repository,
        segmenter,
        message_id="bot",
        author_id="999",
        content="先前的助手回覆",
        minute=1,
        replied_to="root",
        is_bot=True,
    )
    await save_message(
        repository,
        segmenter,
        message_id="secret",
        author_id="1",
        content="[已遮罩]",
        minute=2,
        replied_to="bot",
        is_sensitive=True,
    )
    await save_message(
        repository,
        segmenter,
        message_id="trigger",
        author_id="1",
        content="請接著回答？",
        minute=3,
        replied_to="root",
    )

    context = await ContextBuilder(
        database.session_factory,
        maximum_characters=200,
    ).build("trigger", assistant_author_id="999")

    assert [message.discord_message_id for message in context.messages] == [
        "root",
        "bot",
        "trigger",
    ]
    assert [message.role for message in context.messages] == ["user", "assistant", "user"]
    assert "[已遮罩]" not in " ".join(message.content for message in context.messages)


@pytest.mark.asyncio
async def test_context_never_exceeds_configured_character_limit(database: Database) -> None:
    repository = MessageRepository(database.session_factory)
    segmenter = ConversationSegmenter(database.session_factory)
    await save_message(
        repository,
        segmenter,
        message_id="root",
        author_id="1",
        content="前文" * 100,
        minute=0,
    )
    await save_message(
        repository,
        segmenter,
        message_id="trigger",
        author_id="1",
        content="現在的問題？",
        minute=1,
        replied_to="root",
    )

    context = await ContextBuilder(
        database.session_factory,
        maximum_characters=40,
    ).build("trigger", assistant_author_id="999")

    assert context.character_count == 40
    assert context.messages[-1].discord_message_id == "trigger"
