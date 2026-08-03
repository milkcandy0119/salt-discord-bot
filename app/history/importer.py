"""具有確認字串、費用上限與冪等保護的正式歷史匯入器。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.budget_manager import (
    BudgetExceededError,
    BudgetManager,
    ModelPrice,
    PaidPurpose,
    Reservation,
    Settlement,
)
from app.bot.message_handler import compose_stored_content
from app.conversations.segmenter import ConversationSegmenter
from app.history.analyzer import (
    HistoricalMessageSource,
    HistoryAnalysisReport,
    HistoryAnalyzer,
)
from app.security.sensitive_filter import SensitiveFilter
from app.storage.background_memory import BackgroundMemoryRepository
from app.storage.models import SegmentSummaryRecord, SummaryEmbeddingRecord
from app.storage.repositories import MessageRepository, NewMessage
from app.workers.background_worker import BackgroundWorker

PHASE_SIX_CONFIRMATION = "確認執行階段 6 正式匯入"


class HistoryImportRefusedError(RuntimeError):
    """正式匯入在任何寫入或付費呼叫前被安全規則拒絕。"""


class ApprovedCostExceededError(HistoryImportRefusedError):
    """最新最壞成本高於使用者明確批准的上限。"""


@dataclass(frozen=True, slots=True)
class HistoryImportReport:
    """不含訊息內容與祕密的正式匯入結果。"""

    confirmation_accepted: bool
    approved_cost_microusd: int
    approval_baseline_global_committed_microusd: int
    analysis: HistoryAnalysisReport
    imported_message_count: int
    duplicate_message_count: int
    sensitive_imported_message_count: int
    recovered_pending_segmentation_count: int
    newly_archived_segment_count: int
    enqueued_background_job_count: int
    worker_completed_count: int
    worker_deferred_count: int
    worker_retried_count: int
    worker_failed_count: int
    pending_background_job_count: int
    background_job_status_counts: dict[str, int]
    summary_count: int
    embedding_chunk_count: int
    global_spent_delta_microusd: int
    global_reserved_delta_microusd: int
    background_spent_delta_microusd: int
    background_reserved_delta_microusd: int

    def as_dict(self) -> dict[str, object]:
        """轉為可以安全輸出成 JSON 的純資料。"""

        return asdict(self)


class ApprovedCostBudgetManager:
    """在既有全域預算之外，再限制本次正式匯入的承諾成本。"""

    def __init__(
        self,
        delegate: BudgetManager,
        maximum_cost_microusd: int,
        baseline_global_committed_microusd: int,
    ) -> None:
        self._delegate = delegate
        self._maximum_cost_microusd = maximum_cost_microusd
        self._baseline_committed_microusd = baseline_global_committed_microusd

    async def start(self) -> None:
        """在第一個付費預留前記錄目前全域承諾成本。"""

        snapshot = await self._delegate.get_snapshot()
        phase_committed = (
            snapshot.global_committed_microusd - self._baseline_committed_microusd
        )
        if phase_committed > self._maximum_cost_microusd:
            raise BudgetExceededError("phase_approved_cost")

    async def reserve(
        self,
        *,
        purpose: PaidPurpose,
        price: ModelPrice,
        maximum_input_tokens: int,
        maximum_output_tokens: int,
    ) -> Reservation:
        """先使用正式帳本預留，再撤銷會突破本次批准上限的預留。"""

        reservation = await self._delegate.reserve(
            purpose=purpose,
            price=price,
            maximum_input_tokens=maximum_input_tokens,
            maximum_output_tokens=maximum_output_tokens,
        )
        snapshot = await self._delegate.get_snapshot()
        phase_committed = (
            snapshot.global_committed_microusd - self._baseline_committed_microusd
        )
        if phase_committed <= self._maximum_cost_microusd:
            return reservation
        await self._delegate.release_unbilled(
            reservation.reservation_id,
            error_code="phase_approved_cost_exceeded",
        )
        raise BudgetExceededError("phase_approved_cost")

    async def settle(
        self,
        reservation_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> Settlement:
        """交由正式預算帳本依實際 Token 用量結算。"""

        return await self._delegate.settle(
            reservation_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    async def release_unbilled(self, reservation_id: str, *, error_code: str) -> bool:
        """釋放已知未計費的預留。"""

        return await self._delegate.release_unbilled(
            reservation_id,
            error_code=error_code,
        )

    async def mark_usage_uncertain(self, reservation_id: str, *, error_code: str) -> bool:
        """保留可能已計費的預留，避免將未知費用當成零。"""

        return await self._delegate.mark_usage_uncertain(
            reservation_id,
            error_code=error_code,
        )


class HistoryImporter:
    """先重做唯讀估價，再保存、切段並處理可恢復背景工作。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        source: HistoricalMessageSource,
        analyzer: HistoryAnalyzer,
        message_repository: MessageRepository,
        segmenter: ConversationSegmenter,
        background_repository: BackgroundMemoryRepository,
        background_worker: BackgroundWorker,
        budget_manager: BudgetManager,
        approved_budget_manager: ApprovedCostBudgetManager,
        sensitive_filter: SensitiveFilter,
        max_job_attempts: int,
        maximum_worker_batches: int = 1_000,
    ) -> None:
        self._session_factory = session_factory
        self._source = source
        self._analyzer = analyzer
        self._message_repository = message_repository
        self._segmenter = segmenter
        self._background_repository = background_repository
        self._background_worker = background_worker
        self._budget_manager = budget_manager
        self._approved_budget_manager = approved_budget_manager
        self._sensitive_filter = sensitive_filter
        self._max_job_attempts = max_job_attempts
        self._maximum_worker_batches = maximum_worker_batches

    async def run(
        self,
        *,
        channel_ids: frozenset[int],
        limit_per_channel: int,
        after: datetime | None,
        confirmation: str,
        maximum_approved_cost_microusd: int,
        approval_baseline_global_committed_microusd: int,
    ) -> HistoryImportReport:
        """執行一次可重跑的正式匯入；安全閘門失敗時不寫入資料。"""

        self._validate_request(
            confirmation=confirmation,
            maximum_approved_cost_microusd=maximum_approved_cost_microusd,
            approval_baseline_global_committed_microusd=(
                approval_baseline_global_committed_microusd
            ),
        )
        read_result = await self._source.read(
            channel_ids=channel_ids,
            limit_per_channel=limit_per_channel,
            after=after,
        )
        analysis = await self._analyzer.analyze_read_result(
            read_result,
            channel_ids=channel_ids,
            limit_per_channel=limit_per_channel,
            after=after,
        )
        self._validate_analysis(
            analysis,
            maximum_approved_cost_microusd,
            approval_baseline_global_committed_microusd,
        )

        before = await self._budget_manager.get_snapshot()
        await self._approved_budget_manager.start()
        imported = duplicates = sensitive_imported = 0
        ordered_messages = sorted(
            read_result.messages,
            key=lambda item: (item.created_at, item.discord_message_id),
        )
        for message in ordered_messages:
            content_scan = self._sensitive_filter.scan(
                compose_stored_content(message.content, message.sticker_names)
            )
            display_scan = self._sensitive_filter.scan(
                message.author_display_name or ""
            )
            categories = tuple(
                dict.fromkeys((*content_scan.categories, *display_scan.categories))
            )
            result = await self._message_repository.save(
                NewMessage(
                    discord_message_id=message.discord_message_id,
                    guild_id=message.guild_id,
                    channel_id=message.channel_id,
                    author_id=message.author_id,
                    author_display_name=(
                        display_scan.masked_content
                        if message.author_display_name is not None
                        else None
                    ),
                    content=content_scan.masked_content,
                    discord_created_at=message.created_at,
                    received_at=datetime.now(UTC),
                    replied_to_message_id=message.replied_to_message_id,
                    is_bot=message.is_bot,
                    is_sensitive=bool(categories),
                    sensitive_categories=categories,
                    notifications_required=False,
                )
            )
            if not result.created:
                duplicates += 1
                continue
            imported += 1
            sensitive_imported += int(bool(categories))
            await self._segmenter.assign_message(message.discord_message_id)

        recovered = await self._segmenter.assign_pending_messages()
        archived = await self._segmenter.archive_inactive_segment_ids(datetime.now(UTC))
        enqueued = await self._background_repository.enqueue_archived_channels(
            frozenset(str(channel_id) for channel_id in channel_ids),
            max_attempts=self._max_job_attempts,
        )
        completed = deferred = retried = failed = 0
        for _ in range(self._maximum_worker_batches):
            batch = await self._background_worker.run_once()
            completed += batch.completed
            deferred += batch.deferred
            retried += batch.retried
            failed += batch.failed
            if batch.deferred:
                break
            if not any((batch.completed, batch.retried, batch.failed)):
                break

        after_snapshot = await self._budget_manager.get_snapshot()
        summary_count, embedding_count = await self._memory_counts()
        return HistoryImportReport(
            confirmation_accepted=True,
            approved_cost_microusd=maximum_approved_cost_microusd,
            approval_baseline_global_committed_microusd=(
                approval_baseline_global_committed_microusd
            ),
            analysis=analysis,
            imported_message_count=imported,
            duplicate_message_count=duplicates,
            sensitive_imported_message_count=sensitive_imported,
            recovered_pending_segmentation_count=recovered,
            newly_archived_segment_count=len(archived),
            enqueued_background_job_count=enqueued,
            worker_completed_count=completed,
            worker_deferred_count=deferred,
            worker_retried_count=retried,
            worker_failed_count=failed,
            pending_background_job_count=(
                await self._background_repository.pending_count()
            ),
            background_job_status_counts=(
                await self._background_repository.status_counts()
            ),
            summary_count=summary_count,
            embedding_chunk_count=embedding_count,
            global_spent_delta_microusd=(
                after_snapshot.global_spent_microusd - before.global_spent_microusd
            ),
            global_reserved_delta_microusd=(
                after_snapshot.global_reserved_microusd
                - before.global_reserved_microusd
            ),
            background_spent_delta_microusd=(
                after_snapshot.background_spent_microusd
                - before.background_spent_microusd
            ),
            background_reserved_delta_microusd=(
                after_snapshot.background_reserved_microusd
                - before.background_reserved_microusd
            ),
        )

    @staticmethod
    def _validate_request(
        *,
        confirmation: str,
        maximum_approved_cost_microusd: int,
        approval_baseline_global_committed_microusd: int,
    ) -> None:
        if confirmation != PHASE_SIX_CONFIRMATION:
            raise HistoryImportRefusedError("正式匯入確認文字不完全相符")
        if maximum_approved_cost_microusd <= 0:
            raise HistoryImportRefusedError("批准費用上限必須大於零")
        if approval_baseline_global_committed_microusd < 0:
            raise HistoryImportRefusedError("批准基準承諾成本不得小於零")

    @staticmethod
    def _validate_analysis(
        analysis: HistoryAnalysisReport,
        maximum_approved_cost_microusd: int,
        approval_baseline_global_committed_microusd: int,
    ) -> None:
        if analysis.truncated_channel_ids:
            raise HistoryImportRefusedError("歷史讀取已截斷，拒絕執行不完整匯入")
        already_committed = (
            analysis.global_committed_microusd
            - approval_baseline_global_committed_microusd
        )
        if already_committed < 0:
            raise HistoryImportRefusedError("目前帳本早於本次批准基準，拒絕自動推測")
        if (
            analysis.maximum_total_cost_microusd + already_committed
            > maximum_approved_cost_microusd
        ):
            raise ApprovedCostExceededError(
                "本次既有承諾成本加最新最壞成本超過批准上限："
                f"{already_committed} + {analysis.maximum_total_cost_microusd} > "
                f"{maximum_approved_cost_microusd} microusd"
            )
        if not analysis.maximum_cost_fits_budget:
            raise HistoryImportRefusedError("最新最壞成本超過背景或全域剩餘預算")

    async def _memory_counts(self) -> tuple[int, int]:
        """只回傳摘要與向量分塊數量，不讀取其內容。"""

        async with self._session_factory() as session:
            summary_count = int(
                await session.scalar(
                    select(func.count()).select_from(SegmentSummaryRecord)
                )
                or 0
            )
            embedding_count = int(
                await session.scalar(
                    select(func.count()).select_from(SummaryEmbeddingRecord)
                )
                or 0
            )
        return summary_count, embedding_count
