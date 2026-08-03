from datetime import UTC, datetime, timedelta

import pytest

from app.ai.budget_manager import BudgetManager, ModelPrice
from app.ai.chat_service import ProviderCallError
from app.ai.embedding_service import EmbeddingService, ProviderEmbeddingResponse
from app.ai.summary_service import ProviderSummaryResponse, SummaryService
from app.conversations.segmenter import ConversationSegmenter
from app.security.sensitive_filter import SensitiveFilter
from app.storage.background_memory import BackgroundMemoryRepository
from app.storage.database import Database
from app.storage.repositories import MessageRepository, NewMessage
from app.storage.vector_store import SQLiteVectorStore
from app.workers.background_worker import BackgroundWorker


class FakeSummaryProvider:
    """不連網的固定摘要供應商。"""

    def __init__(self, *, fail_first_unbilled: bool = False) -> None:
        self.calls = 0
        self._fail_first_unbilled = fail_first_unbilled

    async def summarize(self, **kwargs: object) -> ProviderSummaryResponse:
        self.calls += 1
        if self._fail_first_unbilled and self.calls == 1:
            raise ProviderCallError("request_not_sent", usage_may_be_billed=False)
        return ProviderSummaryResponse(
            response_id=f"summary-{self.calls}",
            output_text="Milk 喜歡肉桂捲，這是先前聊過的食物偏好。",
            input_tokens=40,
            output_tokens=20,
        )


class FakeEmbeddingProvider:
    """不連網且輸出三維向量的測試供應商。"""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, **kwargs: object) -> ProviderEmbeddingResponse:
        self.calls += 1
        texts = kwargs["texts"]
        return ProviderEmbeddingResponse(
            vectors=tuple((1.0, 0.0, 0.0) for _ in texts),
            input_tokens=12,
        )


async def _create_archived_segment(
    database: Database,
    *,
    message_id: str,
    channel_id: str = "2",
    created_at: datetime,
) -> int:
    repository = MessageRepository(database.session_factory)
    segmenter = ConversationSegmenter(database.session_factory)
    await repository.save(
        NewMessage(
            discord_message_id=message_id,
            guild_id="1",
            channel_id=channel_id,
            author_id="3",
            author_display_name="Milk",
            content="我喜歡肉桂捲",
            discord_created_at=created_at,
            received_at=created_at,
            replied_to_message_id=None,
            is_bot=False,
            is_sensitive=False,
            sensitive_categories=(),
        )
    )
    assignment = await segmenter.assign_message(message_id)
    archived_ids = await segmenter.archive_inactive_segment_ids(
        created_at + timedelta(minutes=31)
    )
    assert archived_ids == (assignment.segment_id,)
    return assignment.segment_id


def _services(
    database: Database,
    *,
    summary_provider: FakeSummaryProvider,
    embedding_provider: FakeEmbeddingProvider,
) -> tuple[
    BackgroundMemoryRepository,
    SummaryService,
    EmbeddingService,
    SQLiteVectorStore,
]:
    repository = BackgroundMemoryRepository(database.session_factory)
    budget = BudgetManager(database.session_factory)
    vector_store = SQLiteVectorStore(database.session_factory)
    embedding = EmbeddingService(
        provider=embedding_provider,
        repository=repository,
        vector_store=vector_store,
        budget_manager=budget,
        price=ModelPrice("embedding-test", "test-price", 20_000, 0),
        sensitive_filter=SensitiveFilter(),
        dimensions=3,
        chunk_characters=2_000,
        chunk_overlap_characters=200,
    )
    summary = SummaryService(
        provider=summary_provider,
        repository=repository,
        budget_manager=budget,
        price=ModelPrice("summary-test", "test-price", 200_000, 1_250_000),
        sensitive_filter=SensitiveFilter(),
        maximum_output_tokens=300,
        max_job_attempts=5,
    )
    return repository, summary, embedding, vector_store


@pytest.mark.asyncio
async def test_background_pipeline_persists_summary_embedding_and_searches_same_channel(
    database: Database,
) -> None:
    start = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    segment_id = await _create_archived_segment(
        database, message_id="memory-1", created_at=start
    )
    other_channel_segment_id = await _create_archived_segment(
        database,
        message_id="memory-other-channel",
        channel_id="99",
        created_at=start + timedelta(seconds=1),
    )
    summary_provider = FakeSummaryProvider()
    embedding_provider = FakeEmbeddingProvider()
    repository, summary, embedding, vector_store = _services(
        database,
        summary_provider=summary_provider,
        embedding_provider=embedding_provider,
    )
    assert await repository.enqueue_archived_segments(
        (segment_id, other_channel_segment_id),
        max_attempts=5,
        now=datetime.now(UTC) - timedelta(minutes=1),
    ) == 2
    assert await repository.enqueue_archived_segments(
        (segment_id,), max_attempts=5, now=datetime.now(UTC) - timedelta(minutes=1)
    ) == 0
    worker = BackgroundWorker(
        repository=repository,
        summary_service=summary,
        embedding_service=embedding,
        stale_after=timedelta(minutes=5),
        retry_base_delay=timedelta(minutes=1),
        budget_retry_after=timedelta(minutes=5),
        maximum_jobs_per_run=10,
    )

    result = await worker.run_once()
    matches = await vector_store.search(
        (1.0, 0.0, 0.0),
        guild_id="1",
        channel_id="2",
        model_name="embedding-test",
        dimension=3,
        limit=3,
    )

    assert result.completed == 4
    assert await repository.status_counts() == {"completed": 4}
    assert summary_provider.calls == 2
    assert embedding_provider.calls == 2
    assert len(matches) == 1
    assert matches[0].segment_id == segment_id
    assert "肉桂捲" in matches[0].content


@pytest.mark.asyncio
async def test_stale_claim_is_recovered_after_repository_restart(database: Database) -> None:
    start = datetime(2026, 8, 4, 13, 0, tzinfo=UTC)
    segment_id = await _create_archived_segment(
        database, message_id="memory-restart", created_at=start
    )
    repository = BackgroundMemoryRepository(database.session_factory)
    await repository.enqueue_archived_segments(
        (segment_id,), max_attempts=5, now=start + timedelta(minutes=31)
    )
    first = await repository.claim_oldest(
        stale_after=timedelta(minutes=5), now=start + timedelta(minutes=31)
    )
    restarted = BackgroundMemoryRepository(database.session_factory)
    recovered = await restarted.claim_oldest(
        stale_after=timedelta(minutes=5), now=start + timedelta(minutes=37)
    )

    assert first is not None
    assert recovered is not None
    assert recovered.id == first.id
    assert recovered.attempts == 2


@pytest.mark.asyncio
async def test_retrying_bad_job_does_not_block_newer_jobs(database: Database) -> None:
    start = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)
    first_segment = await _create_archived_segment(
        database, message_id="retry-1", created_at=start
    )
    second_segment = await _create_archived_segment(
        database,
        message_id="retry-2",
        channel_id="3",
        created_at=start + timedelta(seconds=1),
    )
    summary_provider = FakeSummaryProvider(fail_first_unbilled=True)
    embedding_provider = FakeEmbeddingProvider()
    repository, summary, embedding, _ = _services(
        database,
        summary_provider=summary_provider,
        embedding_provider=embedding_provider,
    )
    await repository.enqueue_archived_segments(
        (first_segment, second_segment),
        max_attempts=5,
        now=datetime.now(UTC) - timedelta(minutes=1),
    )
    worker = BackgroundWorker(
        repository=repository,
        summary_service=summary,
        embedding_service=embedding,
        stale_after=timedelta(minutes=5),
        retry_base_delay=timedelta(minutes=1),
        budget_retry_after=timedelta(minutes=5),
        maximum_jobs_per_run=10,
    )

    result = await worker.run_once()

    assert result.retried == 1
    assert result.completed == 2
    assert await repository.status_counts() == {"completed": 2, "retry_wait": 1}


@pytest.mark.asyncio
async def test_missing_provider_keeps_job_pending_without_paid_call(database: Database) -> None:
    start = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)
    segment_id = await _create_archived_segment(
        database, message_id="deferred-1", created_at=start
    )
    repository = BackgroundMemoryRepository(database.session_factory)
    await repository.enqueue_archived_segments(
        (segment_id,), max_attempts=5, now=datetime.now(UTC) - timedelta(minutes=1)
    )
    summary = SummaryService(
        provider=None,
        repository=repository,
        budget_manager=BudgetManager(database.session_factory),
        price=ModelPrice("summary-test", "test-price", 200_000, 1_250_000),
        sensitive_filter=SensitiveFilter(),
        maximum_output_tokens=300,
        max_job_attempts=5,
    )
    embedding = EmbeddingService(
        provider=None,
        repository=repository,
        vector_store=SQLiteVectorStore(database.session_factory),
        budget_manager=BudgetManager(database.session_factory),
        price=ModelPrice("embedding-test", "test-price", 20_000, 0),
        sensitive_filter=SensitiveFilter(),
        dimensions=3,
        chunk_characters=2_000,
        chunk_overlap_characters=200,
    )
    worker = BackgroundWorker(
        repository=repository,
        summary_service=summary,
        embedding_service=embedding,
        stale_after=timedelta(minutes=5),
        retry_base_delay=timedelta(minutes=1),
        budget_retry_after=timedelta(minutes=5),
        maximum_jobs_per_run=10,
    )

    result = await worker.run_once()
    snapshot = await BudgetManager(database.session_factory).get_snapshot()

    assert result.deferred == 1
    assert await repository.status_counts() == {"pending": 1}
    assert snapshot.global_spent_microusd == 0
    assert snapshot.global_reserved_microusd == 0
