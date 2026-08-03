from datetime import UTC, datetime, timedelta

import pytest

from app.ai.budget_manager import BudgetManager, ModelPrice
from app.conversations.segmenter import ConversationSegmenter
from app.history.analyzer import (
    HistoricalMessage,
    HistoryAnalyzer,
    HistoryReadResult,
)
from app.security.sensitive_filter import SensitiveFilter
from app.storage.background_memory import BackgroundMemoryRepository
from app.storage.database import Database
from app.storage.repositories import MessageRepository, NewMessage


class FakeHistorySource:
    """回傳固定歷史且不接觸 Discord 的測試來源。"""

    def __init__(self, messages: tuple[HistoricalMessage, ...]) -> None:
        self.messages = messages
        self.calls = 0

    async def read(
        self,
        *,
        channel_ids: frozenset[int],
        limit_per_channel: int,
        after: datetime | None,
    ) -> HistoryReadResult:
        self.calls += 1
        assert channel_ids == frozenset({2})
        assert limit_per_channel == 100
        assert after is None
        return HistoryReadResult(self.messages, ())


async def _seed_archived_message(database: Database, created_at: datetime) -> int:
    repository = MessageRepository(database.session_factory)
    await repository.save(
        NewMessage(
            discord_message_id="existing",
            guild_id="1",
            channel_id="2",
            author_id="10",
            author_display_name="既有成員",
            content="資料庫中尚未摘要的訊息",
            discord_created_at=created_at,
            received_at=created_at,
            replied_to_message_id=None,
            is_bot=False,
            is_sensitive=False,
            sensitive_categories=(),
        )
    )
    segmenter = ConversationSegmenter(database.session_factory)
    assignment = await segmenter.assign_message("existing")
    await segmenter.archive_inactive(created_at + timedelta(minutes=31))
    return assignment.segment_id


def _message(
    message_id: str,
    *,
    content: str,
    minute: int,
) -> HistoricalMessage:
    return HistoricalMessage(
        discord_message_id=message_id,
        guild_id="1",
        channel_id="2",
        author_id="10",
        author_display_name="測試成員",
        content=content,
        created_at=datetime(2026, 8, 4, 12, minute, tzinfo=UTC),
        replied_to_message_id=None,
        is_bot=False,
    )


def _analyzer(database: Database, source: FakeHistorySource) -> HistoryAnalyzer:
    return HistoryAnalyzer(
        database.session_factory,
        source=source,
        budget_manager=BudgetManager(database.session_factory),
        sensitive_filter=SensitiveFilter(),
        summary_price=ModelPrice("summary-test", "price-v1", 200_000, 1_250_000),
        embedding_price=ModelPrice("embedding-test", "price-v1", 20_000, 0),
        summary_max_output_tokens=300,
        implicit_continuation_window=timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_analysis_is_read_only_and_separates_existing_sensitive_and_eligible(
    database: Database,
) -> None:
    start = datetime(2026, 8, 4, 11, 0, tzinfo=UTC)
    await _seed_archived_message(database, start)
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    source = FakeHistorySource(
        (
            _message("existing", content="已經存在", minute=0),
            _message("new-safe", content="新的安全歷史", minute=1),
            _message("new-sensitive", content=secret, minute=2),
        )
    )
    analyzer = _analyzer(database, source)
    message_repository = MessageRepository(database.session_factory)
    before_count = await message_repository.count()

    report = await analyzer.analyze(
        channel_ids=frozenset({2}),
        limit_per_channel=100,
    )

    assert report.fetched_message_count == 3
    assert report.existing_message_count == 1
    assert report.new_message_count == 2
    assert report.sensitive_new_message_count == 1
    assert report.eligible_new_message_count == 1
    assert report.estimated_new_segment_count == 1
    assert report.existing_archived_segments_without_summary == 1
    assert report.estimated_total_cost_microusd > 0
    assert report.maximum_total_cost_microusd >= report.estimated_total_cost_microusd
    assert report.paid_api_calls_made == 0
    assert report.database_writes_made == 0
    assert await message_repository.count() == before_count
    assert await BackgroundMemoryRepository(
        database.session_factory
    ).status_counts() == {}
    snapshot = await BudgetManager(database.session_factory).get_snapshot()
    assert snapshot.global_spent_microusd == 0
    assert snapshot.global_reserved_microusd == 0
    assert secret not in str(report.as_dict())


@pytest.mark.asyncio
async def test_analysis_can_be_repeated_without_creating_duplicate_messages(
    database: Database,
) -> None:
    source = FakeHistorySource((_message("new-safe", content="新的安全歷史", minute=1),))
    analyzer = _analyzer(database, source)

    first = await analyzer.analyze(channel_ids=frozenset({2}), limit_per_channel=100)
    second = await analyzer.analyze(channel_ids=frozenset({2}), limit_per_channel=100)

    assert first.new_message_count == second.new_message_count == 1
    assert first.estimated_total_cost_microusd == second.estimated_total_cost_microusd
    assert await MessageRepository(database.session_factory).count() == 0
    assert source.calls == 2


@pytest.mark.asyncio
async def test_analysis_reports_truncated_channels_and_attachment_capacity(
    database: Database,
) -> None:
    message = HistoricalMessage(
        discord_message_id="with-file",
        guild_id="1",
        channel_id="2",
        author_id="10",
        author_display_name="測試成員",
        content="附件只有中繼資料",
        created_at=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        replied_to_message_id=None,
        is_bot=False,
        attachment_count=2,
        attachment_bytes=12_345,
    )

    class TruncatedSource(FakeHistorySource):
        async def read(self, **kwargs: object) -> HistoryReadResult:
            del kwargs
            return HistoryReadResult((message,), ("2",))

    report = await _analyzer(database, TruncatedSource((message,))).analyze(
        channel_ids=frozenset({2}),
        limit_per_channel=100,
    )

    assert report.truncated_channel_ids == ("2",)
    assert report.attachment_count == 2
    assert report.advertised_attachment_bytes == 12_345


@pytest.mark.asyncio
async def test_analysis_includes_active_segments_that_formal_import_will_archive(
    database: Database,
) -> None:
    """正式匯入會封存的舊活動段落也必須先計入最壞成本。"""

    created_at = datetime.now(UTC) - timedelta(hours=2)
    repository = MessageRepository(database.session_factory)
    await repository.save(
        NewMessage(
            discord_message_id="old-active",
            guild_id="1",
            channel_id="2",
            author_id="10",
            author_display_name="測試成員",
            content="尚未封存但已經逾時",
            discord_created_at=created_at,
            received_at=created_at,
            replied_to_message_id=None,
            is_bot=False,
            is_sensitive=False,
            sensitive_categories=(),
        )
    )
    await ConversationSegmenter(database.session_factory).assign_message("old-active")

    report = await _analyzer(database, FakeHistorySource(())).analyze(
        channel_ids=frozenset({2}),
        limit_per_channel=100,
    )

    assert report.existing_archived_segments_without_summary == 1
    assert report.maximum_total_cost_microusd > 0
