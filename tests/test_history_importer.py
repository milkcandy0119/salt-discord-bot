from datetime import UTC, datetime, timedelta

import pytest

from app.ai.budget_manager import (
    BudgetExceededError,
    BudgetManager,
    ModelPrice,
    PaidPurpose,
)
from app.ai.embedding_service import EmbeddingService, ProviderEmbeddingResponse
from app.ai.summary_service import ProviderSummaryResponse, SummaryService
from app.conversations.segmenter import ConversationSegmenter
from app.history.analyzer import HistoricalMessage, HistoryAnalyzer, HistoryReadResult
from app.history.importer import (
    PHASE_SIX_CONFIRMATION,
    ApprovedCostBudgetManager,
    ApprovedCostExceededError,
    HistoryImporter,
    HistoryImportRefusedError,
)
from app.security.sensitive_filter import SensitiveFilter
from app.storage.background_memory import BackgroundMemoryRepository
from app.storage.database import Database
from app.storage.repositories import MessageRepository
from app.storage.vector_store import SQLiteVectorStore
from app.workers.background_worker import BackgroundWorker


class FakeHistorySource:
    """只回傳固定快照，方便驗證匯入器不會額外讀取 Discord。"""

    def __init__(self, messages: tuple[HistoricalMessage, ...]) -> None:
        self.messages = messages
        self.calls = 0

    async def read(self, **kwargs: object) -> HistoryReadResult:
        self.calls += 1
        assert kwargs["channel_ids"] == frozenset({2})
        return HistoryReadResult(self.messages, ())


class FakeSummaryProvider:
    """不連網的摘要供應商。"""

    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, **kwargs: object) -> ProviderSummaryResponse:
        del kwargs
        self.calls += 1
        return ProviderSummaryResponse(
            response_id=f"summary-{self.calls}",
            output_text="成員分享了今天的餐點。",
            input_tokens=30,
            output_tokens=12,
        )


class FakeEmbeddingProvider:
    """不連網且輸出固定三維向量的供應商。"""

    def __init__(self) -> None:
        self.calls = 0

    async def embed(self, **kwargs: object) -> ProviderEmbeddingResponse:
        self.calls += 1
        texts = kwargs["texts"]
        return ProviderEmbeddingResponse(
            vectors=tuple((1.0, 0.0, 0.0) for _ in texts),
            input_tokens=8,
        )


def _historical_message(
    message_id: str,
    *,
    content: str,
    created_at: datetime,
) -> HistoricalMessage:
    return HistoricalMessage(
        discord_message_id=message_id,
        guild_id="1",
        channel_id="2",
        author_id="3",
        author_display_name="測試成員",
        content=content,
        created_at=created_at,
        replied_to_message_id=None,
        is_bot=False,
    )


def _build_importer(
    database: Database,
    source: FakeHistorySource,
    *,
    approved_cost: int,
) -> tuple[HistoryImporter, FakeSummaryProvider, FakeEmbeddingProvider]:
    sensitive_filter = SensitiveFilter()
    budget_manager = BudgetManager(database.session_factory)
    approved_budget = ApprovedCostBudgetManager(budget_manager, approved_cost, 0)
    summary_price = ModelPrice("summary-test", "price-v1", 200_000, 1_250_000)
    embedding_price = ModelPrice("embedding-test", "price-v1", 20_000, 0)
    analyzer = HistoryAnalyzer(
        database.session_factory,
        source=source,
        budget_manager=budget_manager,
        sensitive_filter=sensitive_filter,
        summary_price=summary_price,
        embedding_price=embedding_price,
        summary_max_output_tokens=300,
        implicit_continuation_window=timedelta(minutes=5),
    )
    repository = BackgroundMemoryRepository(database.session_factory)
    summary_provider = FakeSummaryProvider()
    embedding_provider = FakeEmbeddingProvider()
    summary_service = SummaryService(
        provider=summary_provider,
        repository=repository,
        budget_manager=approved_budget,
        price=summary_price,
        sensitive_filter=sensitive_filter,
        maximum_output_tokens=300,
        max_job_attempts=5,
    )
    embedding_service = EmbeddingService(
        provider=embedding_provider,
        repository=repository,
        vector_store=SQLiteVectorStore(database.session_factory),
        budget_manager=approved_budget,
        price=embedding_price,
        sensitive_filter=sensitive_filter,
        dimensions=3,
        chunk_characters=2_000,
        chunk_overlap_characters=200,
    )
    worker = BackgroundWorker(
        repository=repository,
        summary_service=summary_service,
        embedding_service=embedding_service,
        stale_after=timedelta(minutes=5),
        retry_base_delay=timedelta(minutes=1),
        budget_retry_after=timedelta(minutes=5),
        maximum_jobs_per_run=1,
    )
    importer = HistoryImporter(
        database.session_factory,
        source=source,
        analyzer=analyzer,
        message_repository=MessageRepository(database.session_factory),
        segmenter=ConversationSegmenter(database.session_factory),
        background_repository=repository,
        background_worker=worker,
        budget_manager=budget_manager,
        approved_budget_manager=approved_budget,
        sensitive_filter=sensitive_filter,
        max_job_attempts=5,
        maximum_worker_batches=10,
    )
    return importer, summary_provider, embedding_provider


@pytest.mark.asyncio
async def test_formal_import_masks_sensitive_data_and_is_idempotent(
    database: Database,
) -> None:
    created_at = datetime.now(UTC) - timedelta(hours=2)
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
    source = FakeHistorySource(
        (
            _historical_message("safe", content="我吃了肉桂捲", created_at=created_at),
            _historical_message(
                "sensitive",
                content=secret,
                created_at=created_at + timedelta(minutes=1),
            ),
        )
    )
    importer, summary_provider, embedding_provider = _build_importer(
        database,
        source,
        approved_cost=100_000,
    )

    first = await importer.run(
        channel_ids=frozenset({2}),
        limit_per_channel=100,
        after=None,
        confirmation=PHASE_SIX_CONFIRMATION,
        maximum_approved_cost_microusd=100_000,
        approval_baseline_global_committed_microusd=0,
    )
    second = await importer.run(
        channel_ids=frozenset({2}),
        limit_per_channel=100,
        after=None,
        confirmation=PHASE_SIX_CONFIRMATION,
        maximum_approved_cost_microusd=100_000,
        approval_baseline_global_committed_microusd=0,
    )

    stored = await MessageRepository(database.session_factory).get_by_discord_id(
        "sensitive"
    )
    assert first.imported_message_count == 2
    assert first.sensitive_imported_message_count == 1
    assert first.worker_completed_count == 2
    assert first.pending_background_job_count == 0
    assert first.summary_count == 1
    assert first.embedding_chunk_count == 1
    assert second.imported_message_count == 0
    assert second.duplicate_message_count == 2
    assert summary_provider.calls == 1
    assert embedding_provider.calls == 1
    assert stored is not None
    assert secret not in stored.content
    assert stored.author_notification_status == "not_required"
    assert stored.admin_notification_status == "not_required"


@pytest.mark.asyncio
async def test_cost_increase_is_rejected_before_database_writes_or_provider_calls(
    database: Database,
) -> None:
    source = FakeHistorySource(
        (
            _historical_message(
                "new",
                content="需要摘要的歷史",
                created_at=datetime.now(UTC) - timedelta(hours=2),
            ),
        )
    )
    importer, summary_provider, embedding_provider = _build_importer(
        database,
        source,
        approved_cost=1,
    )

    with pytest.raises(ApprovedCostExceededError):
        await importer.run(
            channel_ids=frozenset({2}),
            limit_per_channel=100,
            after=None,
            confirmation=PHASE_SIX_CONFIRMATION,
            maximum_approved_cost_microusd=1,
            approval_baseline_global_committed_microusd=0,
        )

    assert await MessageRepository(database.session_factory).count() == 0
    assert summary_provider.calls == 0
    assert embedding_provider.calls == 0
    snapshot = await BudgetManager(database.session_factory).get_snapshot()
    assert snapshot.global_spent_microusd == 0
    assert snapshot.global_reserved_microusd == 0


@pytest.mark.asyncio
async def test_wrong_confirmation_is_rejected_before_discord_read(
    database: Database,
) -> None:
    source = FakeHistorySource(())
    importer, summary_provider, embedding_provider = _build_importer(
        database,
        source,
        approved_cost=100_000,
    )

    with pytest.raises(HistoryImportRefusedError):
        await importer.run(
            channel_ids=frozenset({2}),
            limit_per_channel=100,
            after=None,
            confirmation="錯誤確認",
            maximum_approved_cost_microusd=100_000,
            approval_baseline_global_committed_microusd=0,
        )

    assert source.calls == 0
    assert summary_provider.calls == 0
    assert embedding_provider.calls == 0


@pytest.mark.asyncio
async def test_approved_cost_baseline_survives_command_restart(
    database: Database,
) -> None:
    """重建費用閘門時沿用原基準，不能重新取得完整批准額度。"""

    manager = BudgetManager(database.session_factory)
    price = ModelPrice("test", "price-v1", 1_000_000, 0)
    first_gate = ApprovedCostBudgetManager(manager, 1, 0)
    await first_gate.start()
    reservation = await first_gate.reserve(
        purpose=PaidPurpose.SUMMARY,
        price=price,
        maximum_input_tokens=1,
        maximum_output_tokens=0,
    )
    await first_gate.settle(
        reservation.reservation_id,
        input_tokens=1,
        output_tokens=0,
    )

    restarted_gate = ApprovedCostBudgetManager(manager, 1, 0)
    await restarted_gate.start()
    with pytest.raises(BudgetExceededError):
        await restarted_gate.reserve(
            purpose=PaidPurpose.EMBEDDING,
            price=price,
            maximum_input_tokens=1,
            maximum_output_tokens=0,
        )

    snapshot = await manager.get_snapshot()
    assert snapshot.global_spent_microusd == 1
    assert snapshot.global_reserved_microusd == 0
