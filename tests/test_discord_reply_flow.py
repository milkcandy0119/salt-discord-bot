from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.ai.budget_manager import BudgetManager
from app.ai.chat_service import ChatOutcome
from app.bot.client import DiscordAssistantClient
from app.bot.message_handler import IncomingMessage
from app.config import Settings
from app.conversations.context_builder import ChatContext, ContextBuilder
from app.conversations.segmenter import ConversationSegmenter
from app.storage.database import Database
from app.storage.repositories import MessageRepository, NewMessage


@dataclass
class FakeClientUser:
    id: int = 999
    name: str = "測試機器人"
    display_name: str = "測試機器人"


@dataclass
class FakeSentMessage:
    id: int = 200
    created_at: datetime = datetime(2026, 8, 3, 12, 1, tzinfo=UTC)


class FakeDiscordMessage:
    def __init__(self) -> None:
        self.sent_content: str | None = None
        self.mention_author: bool | None = None
        self.allowed_mentions: object | None = None

    async def reply(
        self,
        content: str,
        *,
        mention_author: bool,
        allowed_mentions: object,
    ) -> FakeSentMessage:
        self.sent_content = content
        self.mention_author = mention_author
        self.allowed_mentions = allowed_mentions
        return FakeSentMessage()


@dataclass
class FakeChatService:
    received_context: ChatContext | None = None

    async def generate(self, context: ChatContext) -> ChatOutcome:
        self.received_context = context
        return ChatOutcome("generated", "安全的測試回覆")


@pytest.mark.asyncio
async def test_successful_discord_reply_is_saved_with_reply_relationship(
    database: Database,
) -> None:
    repository = MessageRepository(database.session_factory)
    segmenter = ConversationSegmenter(database.session_factory)
    trigger_time = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    await repository.save(
        NewMessage(
            discord_message_id="100",
            guild_id="1",
            channel_id="2",
            author_id="3",
            author_display_name="測試者",
            content="請幫我回答？",
            discord_created_at=trigger_time,
            received_at=trigger_time,
            replied_to_message_id=None,
            is_bot=False,
            is_sensitive=False,
            sensitive_categories=(),
        )
    )
    await segmenter.assign_message("100")
    chat_service = FakeChatService()
    settings = Settings(
        _env_file=None,
        discord_allowed_guild_ids="1",
        discord_allowed_channel_ids="2",
        discord_owner_user_id="9",
    )
    client = DiscordAssistantClient(
        settings=settings,
        repository=repository,
        segmenter=segmenter,
        budget_manager=BudgetManager(database.session_factory),
        context_builder=ContextBuilder(
            database.session_factory,
            maximum_characters=12_000,
        ),
        chat_service=chat_service,  # type: ignore[arg-type]
    )
    client._connection.user = FakeClientUser()  # type: ignore[assignment]
    discord_message = FakeDiscordMessage()
    incoming = IncomingMessage(
        discord_message_id="100",
        guild_id=1,
        channel_id=2,
        author_id=3,
        author_display_name="測試者",
        content="請幫我回答？",
        discord_created_at=trigger_time,
        replied_to_message_id=None,
        author_is_bot=False,
        is_own_message=False,
    )

    await client._send_ai_reply(  # noqa: SLF001
        discord_message,  # type: ignore[arg-type]
        incoming,
        companion_generated=False,
    )
    stored = await repository.get_by_discord_id("200")

    assert discord_message.sent_content == "安全的測試回覆"
    assert discord_message.mention_author is False
    assert stored is not None
    assert stored.is_bot is True
    assert stored.replied_to_message_id == "100"
    assert stored.segment_id is not None
    assert chat_service.received_context is not None
    await client.close()
