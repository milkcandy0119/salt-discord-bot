"""不呼叫付費 API、也不寫入資料庫的歷史訊息分析器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.budget_manager import (
    BACKGROUND_LIMIT_MICROUSD,
    GLOBAL_LIMIT_MICROUSD,
    BudgetManager,
    ModelPrice,
)
from app.ai.summary_service import SUMMARY_INSTRUCTIONS
from app.bot.message_handler import compose_stored_content
from app.security.sensitive_filter import SensitiveFilter
from app.storage.models import (
    BackgroundJobRecord,
    ConversationSegmentRecord,
    MessageRecord,
    SegmentSummaryRecord,
)

ANALYSIS_VERSION = "history-analysis-v1"


@dataclass(frozen=True, slots=True)
class HistoricalMessage:
    """與 Discord.py 解耦的唯讀歷史訊息快照。"""

    discord_message_id: str
    guild_id: str
    channel_id: str
    author_id: str
    author_display_name: str | None
    content: str
    created_at: datetime
    replied_to_message_id: str | None
    is_bot: bool
    sticker_names: tuple[str, ...] = ()
    attachment_count: int = 0
    attachment_bytes: int = 0


@dataclass(frozen=True, slots=True)
class HistoryReadResult:
    """歷史來源讀取結果與截斷資訊。"""

    messages: tuple[HistoricalMessage, ...]
    truncated_channel_ids: tuple[str, ...]


class HistoricalMessageSource(Protocol):
    """可由測試替身取代的唯讀歷史來源。"""

    async def read(
        self,
        *,
        channel_ids: frozenset[int],
        limit_per_channel: int,
        after: datetime | None,
    ) -> HistoryReadResult: ...


@dataclass(frozen=True, slots=True)
class HistoryAnalysisReport:
    """不包含訊息內容、作者名稱或祕密的免費分析報告。"""

    analysis_version: str
    generated_at: str
    channel_count: int
    limit_per_channel: int
    after: str | None
    truncated_channel_ids: tuple[str, ...]
    fetched_message_count: int
    existing_message_count: int
    new_message_count: int
    sensitive_new_message_count: int
    eligible_new_message_count: int
    estimated_new_segment_count: int
    maximum_new_segment_count: int
    existing_archived_segments_without_summary: int
    pending_background_job_count: int
    text_character_count: int
    text_utf8_bytes: int
    conservative_text_token_upper_bound: int
    attachment_count: int
    advertised_attachment_bytes: int
    summary_model: str
    summary_price_version: str
    embedding_model: str
    embedding_price_version: str
    estimated_summary_cost_microusd: int
    estimated_embedding_cost_microusd: int
    estimated_total_cost_microusd: int
    maximum_total_cost_microusd: int
    background_remaining_microusd: int
    global_remaining_microusd: int
    estimated_cost_fits_budget: bool
    maximum_cost_fits_budget: bool
    paid_api_calls_made: int = 0
    database_writes_made: int = 0

    def as_dict(self) -> dict[str, object]:
        """轉成可安全輸出 JSON 的純量資料。"""

        return {
            field: getattr(self, field)
            for field in self.__dataclass_fields__
        }


@dataclass(slots=True)
class _EstimatedSegment:
    """只存在記憶體中的免費切段估算狀態。"""

    id: int
    channel_id: str
    last_message_at: datetime
    author_last_seen: dict[str, datetime]
    messages: list[HistoricalMessage]


class HistoryAnalyzer:
    """讀取白名單歷史後，只做本機統計、切段模擬與價格估算。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        source: HistoricalMessageSource,
        budget_manager: BudgetManager,
        sensitive_filter: SensitiveFilter,
        summary_price: ModelPrice,
        embedding_price: ModelPrice,
        summary_max_output_tokens: int,
        implicit_continuation_window: timedelta,
    ) -> None:
        self._session_factory = session_factory
        self._source = source
        self._budget_manager = budget_manager
        self._sensitive_filter = sensitive_filter
        self._summary_price = summary_price
        self._embedding_price = embedding_price
        self._summary_max_output_tokens = summary_max_output_tokens
        self._implicit_continuation_window = implicit_continuation_window

    async def analyze(
        self,
        *,
        channel_ids: frozenset[int],
        limit_per_channel: int,
        after: datetime | None = None,
    ) -> HistoryAnalysisReport:
        """執行唯讀分析；不保存訊息、不排工作、不預留預算。"""

        if not channel_ids:
            raise ValueError("歷史分析至少需要一個允許頻道")
        if limit_per_channel <= 0:
            raise ValueError("每頻道分析上限必須大於零")
        read_result = await self._source.read(
            channel_ids=channel_ids,
            limit_per_channel=limit_per_channel,
            after=after,
        )
        messages = tuple(
            sorted(
                read_result.messages,
                key=lambda item: (item.created_at, item.discord_message_id),
            )
        )
        existing_ids = await self._existing_message_ids(
            tuple(message.discord_message_id for message in messages)
        )
        new_messages = tuple(
            message
            for message in messages
            if message.discord_message_id not in existing_ids
        )
        eligible: list[HistoricalMessage] = []
        sensitive_count = 0
        text_character_count = 0
        text_utf8_bytes = 0
        attachment_count = 0
        attachment_bytes = 0
        for message in new_messages:
            stored_content = compose_stored_content(message.content, message.sticker_names)
            content_scan = self._sensitive_filter.scan(stored_content)
            display_scan = self._sensitive_filter.scan(message.author_display_name or "")
            is_sensitive = content_scan.is_sensitive or display_scan.is_sensitive
            sensitive_count += int(is_sensitive)
            attachment_count += message.attachment_count
            attachment_bytes += message.attachment_bytes
            if is_sensitive:
                continue
            sanitized = HistoricalMessage(
                discord_message_id=message.discord_message_id,
                guild_id=message.guild_id,
                channel_id=message.channel_id,
                author_id=message.author_id,
                author_display_name=message.author_display_name,
                content=stored_content,
                created_at=self._as_utc(message.created_at),
                replied_to_message_id=message.replied_to_message_id,
                is_bot=message.is_bot,
                sticker_names=(),
                attachment_count=message.attachment_count,
                attachment_bytes=message.attachment_bytes,
            )
            eligible.append(sanitized)
            text_character_count += len(stored_content)
            text_utf8_bytes += len(stored_content.encode("utf-8"))

        estimated_segments = self._estimate_segments(tuple(eligible))
        existing_sources = await self._existing_unsummarized_sources(
            frozenset(str(channel_id) for channel_id in channel_ids)
        )
        pending_jobs = await self._pending_background_job_count()
        estimated_summary_sources = (
            *existing_sources,
            *(tuple(segment.messages) for segment in estimated_segments),
        )
        maximum_summary_sources = (
            *existing_sources,
            *((message,) for message in eligible),
        )
        estimated_summary_cost, estimated_embedding_cost = self._estimate_costs(
            estimated_summary_sources
        )
        maximum_summary_cost, maximum_embedding_cost = self._estimate_costs(
            maximum_summary_sources
        )
        snapshot = await self._budget_manager.get_snapshot()
        background_remaining = max(
            BACKGROUND_LIMIT_MICROUSD
            - snapshot.background_spent_microusd
            - snapshot.background_reserved_microusd,
            0,
        )
        global_remaining = max(
            GLOBAL_LIMIT_MICROUSD
            - snapshot.global_spent_microusd
            - snapshot.global_reserved_microusd,
            0,
        )
        estimated_total = estimated_summary_cost + estimated_embedding_cost
        maximum_total = maximum_summary_cost + maximum_embedding_cost
        return HistoryAnalysisReport(
            analysis_version=ANALYSIS_VERSION,
            generated_at=datetime.now(UTC).isoformat(),
            channel_count=len(channel_ids),
            limit_per_channel=limit_per_channel,
            after=after.isoformat() if after is not None else None,
            truncated_channel_ids=read_result.truncated_channel_ids,
            fetched_message_count=len(messages),
            existing_message_count=len(existing_ids),
            new_message_count=len(new_messages),
            sensitive_new_message_count=sensitive_count,
            eligible_new_message_count=len(eligible),
            estimated_new_segment_count=len(estimated_segments),
            maximum_new_segment_count=len(eligible),
            existing_archived_segments_without_summary=len(existing_sources),
            pending_background_job_count=pending_jobs,
            text_character_count=text_character_count,
            text_utf8_bytes=text_utf8_bytes,
            conservative_text_token_upper_bound=text_utf8_bytes,
            attachment_count=attachment_count,
            advertised_attachment_bytes=attachment_bytes,
            summary_model=self._summary_price.model_name,
            summary_price_version=self._summary_price.price_version,
            embedding_model=self._embedding_price.model_name,
            embedding_price_version=self._embedding_price.price_version,
            estimated_summary_cost_microusd=estimated_summary_cost,
            estimated_embedding_cost_microusd=estimated_embedding_cost,
            estimated_total_cost_microusd=estimated_total,
            maximum_total_cost_microusd=maximum_total,
            background_remaining_microusd=background_remaining,
            global_remaining_microusd=global_remaining,
            estimated_cost_fits_budget=(
                estimated_total <= background_remaining
                and estimated_total <= global_remaining
            ),
            maximum_cost_fits_budget=(
                maximum_total <= background_remaining
                and maximum_total <= global_remaining
            ),
        )

    async def _existing_message_ids(self, message_ids: tuple[str, ...]) -> frozenset[str]:
        """分批查詢既有 Discord ID，避免 SQLite 變數數量上限。"""

        existing: set[str] = set()
        async with self._session_factory() as session:
            for start in range(0, len(message_ids), 500):
                batch = message_ids[start : start + 500]
                if not batch:
                    continue
                rows = await session.scalars(
                    select(MessageRecord.discord_message_id).where(
                        MessageRecord.discord_message_id.in_(batch)
                    )
                )
                existing.update(rows.all())
        return frozenset(existing)

    async def _existing_unsummarized_sources(
        self,
        channel_ids: frozenset[str],
    ) -> tuple[tuple[HistoricalMessage, ...], ...]:
        """讀取已封存但尚無摘要的非敏感來源，仍不建立任何工作。"""

        summary_exists = select(SegmentSummaryRecord.id).where(
            SegmentSummaryRecord.segment_id == ConversationSegmentRecord.id
        ).exists()
        async with self._session_factory() as session:
            segment_ids = (
                await session.scalars(
                    select(ConversationSegmentRecord.id)
                    .where(
                        ConversationSegmentRecord.channel_id.in_(channel_ids),
                        ConversationSegmentRecord.status == "archived",
                        ~summary_exists,
                    )
                    .order_by(ConversationSegmentRecord.created_at)
                )
            ).all()
            sources: list[tuple[HistoricalMessage, ...]] = []
            for segment_id in segment_ids:
                rows = (
                    await session.scalars(
                        select(MessageRecord)
                        .where(
                            MessageRecord.segment_id == segment_id,
                            MessageRecord.is_sensitive.is_(False),
                        )
                        .order_by(MessageRecord.discord_created_at, MessageRecord.id)
                    )
                ).all()
                if not rows:
                    continue
                sources.append(
                    tuple(
                        HistoricalMessage(
                            discord_message_id=row.discord_message_id,
                            guild_id=row.guild_id,
                            channel_id=row.channel_id,
                            author_id=row.author_id,
                            author_display_name=row.author_display_name,
                            content=row.content,
                            created_at=self._as_utc(row.discord_created_at),
                            replied_to_message_id=row.replied_to_message_id,
                            is_bot=row.is_bot,
                        )
                        for row in rows
                    )
                )
        return tuple(sources)

    async def _pending_background_job_count(self) -> int:
        """唯讀計算已存在且尚未完成的背景工作。"""

        async with self._session_factory() as session:
            return int(
                await session.scalar(
                    select(func.count())
                    .select_from(BackgroundJobRecord)
                    .where(
                        BackgroundJobRecord.status.in_(
                            ("pending", "retry_wait", "processing")
                        )
                    )
                )
                or 0
            )

    def _estimate_segments(
        self,
        messages: tuple[HistoricalMessage, ...],
    ) -> tuple[_EstimatedSegment, ...]:
        """在記憶體模擬回覆優先與保守續接規則。"""

        active: dict[int, _EstimatedSegment] = {}
        all_segments: list[_EstimatedSegment] = []
        message_segment: dict[str, _EstimatedSegment] = {}
        next_id = 1
        for message in messages:
            message_time = self._as_utc(message.created_at)
            for segment_id, segment in tuple(active.items()):
                if segment.last_message_at <= message_time - timedelta(minutes=30):
                    active.pop(segment_id)
            segment = None
            if message.replied_to_message_id is not None:
                replied_segment = message_segment.get(message.replied_to_message_id)
                if (
                    replied_segment is not None
                    and replied_segment.channel_id == message.channel_id
                ):
                    segment = replied_segment
                    active[segment.id] = segment
            if segment is None:
                cutoff = message_time - self._implicit_continuation_window
                candidates = [
                    item
                    for item in active.values()
                    if item.channel_id == message.channel_id
                    and item.author_last_seen.get(
                        message.author_id,
                        datetime.min.replace(tzinfo=UTC),
                    )
                    >= cutoff
                ]
                if len(candidates) == 1:
                    segment = candidates[0]
            if segment is None:
                segment = _EstimatedSegment(
                    id=next_id,
                    channel_id=message.channel_id,
                    last_message_at=message_time,
                    author_last_seen={},
                    messages=[],
                )
                next_id += 1
                active[segment.id] = segment
                all_segments.append(segment)
            segment.last_message_at = max(segment.last_message_at, message_time)
            segment.author_last_seen[message.author_id] = message_time
            segment.messages.append(message)
            message_segment[message.discord_message_id] = segment
        return tuple(all_segments)

    def _estimate_costs(
        self,
        sources: tuple[tuple[HistoricalMessage, ...], ...],
    ) -> tuple[int, int]:
        """逐次呼叫向上取整，避免低估小型段落的計費。"""

        summary_cost = 0
        embedding_cost = 0
        instruction_bytes = len(SUMMARY_INSTRUCTIONS.encode("utf-8"))
        for source in sources:
            rendered_bytes = sum(
                len(
                    (
                        f"{message.author_display_name or f'使用者 {message.author_id}'}: "
                        f"{message.content.strip()}"
                    ).encode()
                )
                for message in source
            )
            summary_cost += self._summary_price.quote(
                input_tokens=rendered_bytes + instruction_bytes + 512,
                output_tokens=self._summary_max_output_tokens,
            )
            embedding_cost += self._embedding_price.quote(
                input_tokens=self._summary_max_output_tokens,
                output_tokens=0,
            )
        return summary_cost, embedding_cost

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
