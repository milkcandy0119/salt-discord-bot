"""階段 5 背景工作、摘要與向量資料存取。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import (
    BackgroundJobRecord,
    ConversationSegmentRecord,
    MessageRecord,
    SegmentSummaryRecord,
    SummaryEmbeddingRecord,
)


@dataclass(frozen=True, slots=True)
class BackgroundJob:
    """工作器可安全攜出的背景工作快照。"""

    id: int
    job_type: str
    segment_id: int | None
    summary_id: int | None
    source_through_message_record_id: int | None
    attempts: int
    max_attempts: int


@dataclass(frozen=True, slots=True)
class SummarySource:
    """摘要服務使用的固定原始訊息範圍。"""

    segment_id: int
    source_through_message_record_id: int
    messages: tuple[MessageRecord, ...]


class BackgroundMemoryRepository:
    """提供可恢復、最舊優先且冪等的背景資料操作。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def enqueue_archived_segments(
        self,
        segment_ids: tuple[int, ...],
        *,
        max_attempts: int,
        now: datetime | None = None,
    ) -> int:
        """只為這次剛封存的段落建立摘要工作，不回填既有封存資料。"""

        if not segment_ids:
            return 0
        effective_now = now or datetime.now(UTC)
        created = 0
        async with self._session_factory() as session, session.begin():
            for segment_id in segment_ids:
                source_through = await session.scalar(
                    select(func.max(MessageRecord.id))
                    .join(
                        ConversationSegmentRecord,
                        ConversationSegmentRecord.id == MessageRecord.segment_id,
                    )
                    .where(
                        MessageRecord.segment_id == segment_id,
                        MessageRecord.is_sensitive.is_(False),
                        ConversationSegmentRecord.status == "archived",
                    )
                )
                if source_through is None:
                    continue
                key = f"summarize_segment:{segment_id}:{source_through}"
                result = await session.execute(
                    sqlite_insert(BackgroundJobRecord)
                    .values(
                        job_type="summarize_segment",
                        segment_id=segment_id,
                        source_through_message_record_id=source_through,
                        idempotency_key=key,
                        status="pending",
                        attempts=0,
                        max_attempts=max_attempts,
                        available_at=effective_now,
                        created_at=effective_now,
                        updated_at=effective_now,
                    )
                    .on_conflict_do_nothing(index_elements=["idempotency_key"])
                )
                created += int(result.rowcount == 1)
        return created

    async def enqueue_archived_channels(
        self,
        channel_ids: frozenset[str],
        *,
        max_attempts: int,
        now: datetime | None = None,
    ) -> int:
        """為指定頻道全部已封存段落冪等補建摘要工作。"""

        if not channel_ids:
            return 0
        async with self._session_factory() as session:
            segment_ids = tuple(
                (
                    await session.scalars(
                        select(ConversationSegmentRecord.id)
                        .where(
                            ConversationSegmentRecord.channel_id.in_(channel_ids),
                            ConversationSegmentRecord.status == "archived",
                        )
                        .order_by(
                            ConversationSegmentRecord.created_at,
                            ConversationSegmentRecord.id,
                        )
                    )
                ).all()
            )
        return await self.enqueue_archived_segments(
            segment_ids,
            max_attempts=max_attempts,
            now=now,
        )

    async def claim_oldest(
        self,
        *,
        stale_after: timedelta,
        now: datetime | None = None,
    ) -> BackgroundJob | None:
        """回收逾時工作後，以原子更新領取最舊的可執行工作。"""

        effective_now = now or datetime.now(UTC)
        ready_statuses = ("pending", "retry_wait")
        candidate = (
            select(BackgroundJobRecord.id)
            .where(
                BackgroundJobRecord.status.in_(ready_statuses),
                BackgroundJobRecord.available_at <= effective_now,
            )
            .order_by(BackgroundJobRecord.created_at, BackgroundJobRecord.id)
            .limit(1)
            .scalar_subquery()
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(BackgroundJobRecord)
                .where(
                    BackgroundJobRecord.status == "processing",
                    BackgroundJobRecord.claimed_at < effective_now - stale_after,
                )
                .values(
                    status="retry_wait",
                    claimed_at=None,
                    available_at=effective_now,
                    last_error_code="stale_claim_recovered",
                    updated_at=effective_now,
                )
            )
            record = (
                await session.execute(
                    update(BackgroundJobRecord)
                    .where(
                        BackgroundJobRecord.id == candidate,
                        BackgroundJobRecord.status.in_(ready_statuses),
                        BackgroundJobRecord.available_at <= effective_now,
                    )
                    .values(
                        status="processing",
                        attempts=BackgroundJobRecord.attempts + 1,
                        claimed_at=effective_now,
                        last_error_code=None,
                        updated_at=effective_now,
                    )
                    .returning(BackgroundJobRecord)
                )
            ).scalar_one_or_none()
        return self._to_job(record) if record is not None else None

    async def mark_completed(self, job_id: int, *, now: datetime | None = None) -> None:
        """將已處理工作標成完成。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(BackgroundJobRecord)
                .where(
                    BackgroundJobRecord.id == job_id,
                    BackgroundJobRecord.status == "processing",
                )
                .values(
                    status="completed",
                    claimed_at=None,
                    completed_at=effective_now,
                    updated_at=effective_now,
                )
            )

    async def defer_for_budget(
        self,
        job_id: int,
        *,
        retry_after: timedelta,
        now: datetime | None = None,
    ) -> None:
        """額度不足時保留 pending，並取消這次未發送 API 的嘗試計數。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(BackgroundJobRecord)
                .where(
                    BackgroundJobRecord.id == job_id,
                    BackgroundJobRecord.status == "processing",
                )
                .values(
                    status="pending",
                    attempts=func.max(BackgroundJobRecord.attempts - 1, 0),
                    available_at=effective_now + retry_after,
                    claimed_at=None,
                    last_error_code="budget_exhausted",
                    updated_at=effective_now,
                )
            )

    async def retry_or_fail(
        self,
        job: BackgroundJob,
        *,
        error_code: str,
        base_delay: timedelta,
        now: datetime | None = None,
    ) -> str:
        """以有上限的指數退避重試，超過次數後隔離壞工作。"""

        effective_now = now or datetime.now(UTC)
        failed = job.attempts >= job.max_attempts
        exponent = max(job.attempts - 1, 0)
        delay_seconds = min(
            base_delay.total_seconds() * (2**exponent),
            timedelta(hours=1).total_seconds(),
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(BackgroundJobRecord)
                .where(
                    BackgroundJobRecord.id == job.id,
                    BackgroundJobRecord.status == "processing",
                )
                .values(
                    status="failed" if failed else "retry_wait",
                    available_at=effective_now + timedelta(seconds=delay_seconds),
                    claimed_at=None,
                    last_error_code=error_code[:64],
                    updated_at=effective_now,
                )
            )
        return "failed" if failed else "retry_wait"

    async def mark_failed(
        self,
        job_id: int,
        *,
        error_code: str,
        now: datetime | None = None,
    ) -> None:
        """隔離不應自動重試的工作，例如付費用量不明。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(BackgroundJobRecord)
                .where(
                    BackgroundJobRecord.id == job_id,
                    BackgroundJobRecord.status == "processing",
                )
                .values(
                    status="failed",
                    claimed_at=None,
                    last_error_code=error_code[:64],
                    updated_at=effective_now,
                )
            )

    async def load_summary_source(self, job: BackgroundJob) -> SummarySource:
        """依工作建立時的上界重新讀取並排除敏感訊息。"""

        if job.segment_id is None or job.source_through_message_record_id is None:
            raise ValueError("摘要工作缺少來源範圍")
        async with self._session_factory() as session:
            segment = await session.get(ConversationSegmentRecord, job.segment_id)
            if segment is None:
                raise LookupError("找不到摘要工作的段落")
            messages = (
                await session.scalars(
                    select(MessageRecord)
                    .where(
                        MessageRecord.segment_id == job.segment_id,
                        MessageRecord.id <= job.source_through_message_record_id,
                        MessageRecord.is_sensitive.is_(False),
                    )
                    .order_by(MessageRecord.discord_created_at, MessageRecord.id)
                )
            ).all()
        if not messages:
            raise ValueError("段落沒有可摘要的非敏感訊息")
        return SummarySource(
            segment_id=job.segment_id,
            source_through_message_record_id=job.source_through_message_record_id,
            messages=tuple(messages),
        )

    async def find_summary(
        self,
        *,
        segment_id: int,
        source_through_message_record_id: int,
        model_name: str,
        prompt_version: str,
    ) -> SegmentSummaryRecord | None:
        """尋找已存在的同版本摘要，供重跑時直接完成。"""

        async with self._session_factory() as session:
            return await session.scalar(
                select(SegmentSummaryRecord).where(
                    SegmentSummaryRecord.segment_id == segment_id,
                    SegmentSummaryRecord.source_through_message_record_id
                    == source_through_message_record_id,
                    SegmentSummaryRecord.model_name == model_name,
                    SegmentSummaryRecord.prompt_version == prompt_version,
                )
            )

    async def store_summary_and_enqueue_embedding(
        self,
        *,
        source: SummarySource,
        content: str,
        model_name: str,
        prompt_version: str,
        provider_response_id: str,
        input_tokens: int,
        output_tokens: int,
        max_attempts: int,
        now: datetime | None = None,
    ) -> int:
        """原子保存摘要並冪等建立後續向量化工作。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            await session.execute(
                sqlite_insert(SegmentSummaryRecord)
                .values(
                    segment_id=source.segment_id,
                    source_through_message_record_id=(
                        source.source_through_message_record_id
                    ),
                    source_message_count=len(source.messages),
                    content=content,
                    model_name=model_name,
                    prompt_version=prompt_version,
                    provider_response_id=provider_response_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    created_at=effective_now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        "segment_id",
                        "source_through_message_record_id",
                        "model_name",
                        "prompt_version",
                    ]
                )
            )
            summary_id = await session.scalar(
                select(SegmentSummaryRecord.id).where(
                    SegmentSummaryRecord.segment_id == source.segment_id,
                    SegmentSummaryRecord.source_through_message_record_id
                    == source.source_through_message_record_id,
                    SegmentSummaryRecord.model_name == model_name,
                    SegmentSummaryRecord.prompt_version == prompt_version,
                )
            )
            if summary_id is None:
                raise RuntimeError("摘要保存後無法讀回")
            key = f"embed_summary:{summary_id}"
            await session.execute(
                sqlite_insert(BackgroundJobRecord)
                .values(
                    job_type="embed_summary",
                    summary_id=summary_id,
                    idempotency_key=key,
                    status="pending",
                    attempts=0,
                    max_attempts=max_attempts,
                    available_at=effective_now,
                    created_at=effective_now,
                    updated_at=effective_now,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
        return summary_id

    async def get_summary(self, summary_id: int) -> SegmentSummaryRecord | None:
        """讀取一筆摘要。"""

        async with self._session_factory() as session:
            return await session.get(SegmentSummaryRecord, summary_id)

    async def embeddings_exist(
        self,
        *,
        summary_id: int,
        model_name: str,
        dimension: int,
    ) -> bool:
        """檢查摘要是否已有相同模型及維度的向量。"""

        async with self._session_factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(SummaryEmbeddingRecord)
                .where(
                    SummaryEmbeddingRecord.summary_id == summary_id,
                    SummaryEmbeddingRecord.model_name == model_name,
                    SummaryEmbeddingRecord.dimension == dimension,
                )
            )
            return bool(count)

    async def pending_count(self) -> int:
        """計算尚待執行或等待重試的工作數。"""

        async with self._session_factory() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(BackgroundJobRecord)
                    .where(BackgroundJobRecord.status.in_(("pending", "retry_wait")))
                )
                or 0
            )

    async def status_counts(self) -> dict[str, int]:
        """供驗收與管理查詢不含內容的工作狀態。"""

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(BackgroundJobRecord.status, func.count())
                    .group_by(BackgroundJobRecord.status)
                    .order_by(BackgroundJobRecord.status)
                )
            ).all()
        return {status: int(count) for status, count in rows}

    @staticmethod
    def _to_job(record: BackgroundJobRecord) -> BackgroundJob:
        return BackgroundJob(
            id=record.id,
            job_type=record.job_type,
            segment_id=record.segment_id,
            summary_id=record.summary_id,
            source_through_message_record_id=record.source_through_message_record_id,
            attempts=record.attempts,
            max_attempts=record.max_attempts,
        )
