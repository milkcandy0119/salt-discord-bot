from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.ai.budget_manager import BudgetManager
from app.ai.chat_service import ChatOutcome
from app.ai.persona import Persona
from app.bot.client import DiscordAssistantClient
from app.bot.message_handler import IncomingMessage
from app.config import Settings
from app.conversations.context_builder import ChatContext, ContextBuilder
from app.conversations.segmenter import ConversationSegmenter
from app.memory.personal_memory import MemoryCaptureOutcome
from app.storage.database import Database
from app.storage.repositories import MessageRepository, NewMessage
from app.storage.trial import TrialRepository
from app.vision.models import IncomingVisual, VisualMediaKind


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
        self.delivery_mode: str | None = None
        self.channel = FakeDiscordChannel(self)

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
        self.delivery_mode = "reply"
        return FakeSentMessage()


class FakeDiscordChannel:
    def __init__(self, message: FakeDiscordMessage) -> None:
        self._message = message

    async def send(
        self,
        content: str,
        *,
        allowed_mentions: object,
    ) -> FakeSentMessage:
        self._message.sent_content = content
        self._message.allowed_mentions = allowed_mentions
        self._message.delivery_mode = "channel"
        return FakeSentMessage()


@dataclass
class FakeChatService:
    received_context: ChatContext | None = None
    received_visual_count: int = 0

    async def generate(self, context: ChatContext, **options: object) -> ChatOutcome:
        self.received_context = context
        visual_inputs = options.get("visual_inputs", ())
        self.received_visual_count = len(visual_inputs)  # type: ignore[arg-type]
        return ChatOutcome("generated", "安全的測試回覆")


def make_visual(resource_id: str) -> IncomingVisual:
    return IncomingVisual(
        resource_id=resource_id,
        media_kind=VisualMediaKind.STICKER,
        filename=f"sticker-{resource_id}.png",
        declared_content_type="image/png",
        declared_size=100,
        source_url=f"https://cdn.discordapp.com/stickers/{resource_id}.png",
    )


def test_reply_target_visuals_are_prioritized_and_deduplicated() -> None:
    target = make_visual("target")
    duplicate = make_visual("target")
    current = make_visual("current")

    visuals = DiscordAssistantClient._merge_visual_inputs(  # noqa: SLF001
        (duplicate, current),
        (target,),
    )

    assert [visual.resource_id for visual in visuals] == ["target", "current"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            "ambiguous_delete",
            "可以喵，不過 Salt 還不知道你指的是哪一筆\n"
            "請先用 /memory menu 查看編號，再透過選單刪除",
        ),
        (
            "unsupported_memory_subject",
            "這比較像群組稱號或別人的資料喵，目前 Salt 只能保存你自己的個人資料，"
            "所以這次沒有存進記憶",
        ),
    ],
)
async def test_non_executed_memory_operations_receive_fixed_reply(
    status: str,
    expected: str,
) -> None:
    message = FakeDiscordMessage()

    await DiscordAssistantClient._send_memory_event_reply(  # type: ignore[arg-type]  # noqa: SLF001
        None,
        message,
        MemoryCaptureOutcome(status),
    )

    assert message.sent_content == expected
    assert message.mention_author is False


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_to_message", [True, False])
async def test_successful_discord_message_preserves_actual_delivery_relationship(
    database: Database,
    reply_to_message: bool,
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
    trial_repository = TrialRepository(database.session_factory)
    await trial_repository.start(
        guild_ids=frozenset({1}),
        channel_ids=frozenset({2}),
        companion_channel_ids=frozenset(),
        timezone_name="Asia/Taipei",
            duration=timedelta(days=3_650),
        global_increment_limit_microusd=1_000_000,
        background_increment_limit_microusd=250_000,
        companion_daily_reply_limit=20,
        now=trigger_time,
    )
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
        persona=Persona(
            identifier="test",
            version="v1",
            display_name="測試機器人",
            instructions="測試人設",
        ),
        trial_repository=trial_repository,
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
        companion_generated=not reply_to_message,
        reply_to_message=reply_to_message,
    )
    stored = await repository.get_by_discord_id("200")

    assert discord_message.sent_content == "安全的測試回覆"
    assert discord_message.delivery_mode == (
        "reply" if reply_to_message else "channel"
    )
    assert discord_message.mention_author is (
        False if reply_to_message else None
    )
    assert stored is not None
    assert stored.is_bot is True
    assert stored.replied_to_message_id == (
        "100" if reply_to_message else None
    )
    assert stored.segment_id is not None
    assert chat_service.received_context is not None
    report = await trial_repository.report()
    expected_reason = (
        "discord_reply_saved"
        if reply_to_message
        else "discord_channel_message_saved"
    )
    assert report["event_counts"] == [
        {
            "event_type": "reply_result",
            "reason": expected_reason,
            "outcome": "generated",
            "count": 1,
        }
    ]
    assert report["reply_latency_ms"]["count"] == 1  # type: ignore[index]
    await client.close()
