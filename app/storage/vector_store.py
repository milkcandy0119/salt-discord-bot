"""使用 SQLite BLOB 與 Python 精確餘弦相似度的向量索引。"""

from __future__ import annotations

import hashlib
import math
from array import array
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import (
    ConversationSegmentRecord,
    MessageRecord,
    SegmentSummaryRecord,
    SummaryEmbeddingRecord,
)


@dataclass(frozen=True, slots=True)
class HistoricalSummary:
    """可放入聊天上下文的歷史摘要及可核對的原始發言。"""

    summary_id: int
    segment_id: int
    content: str
    score: float
    channel_id: str = ""
    evidence: str = ""


class SQLiteVectorStore:
    """以完整 1536 維向量進行適合小型資料庫的精確掃描。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def store(
        self,
        *,
        summary_id: int,
        chunks: tuple[str, ...],
        vectors: tuple[tuple[float, ...], ...],
        model_name: str,
        dimension: int,
        now: datetime | None = None,
    ) -> None:
        """驗證向量後冪等保存每個摘要分塊。"""

        if len(chunks) != len(vectors) or not chunks:
            raise ValueError("摘要分塊與向量數量必須相同且不得為空")
        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            for chunk_index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                if len(vector) != dimension:
                    raise ValueError("Embedding 維度與設定不符")
                norm = math.sqrt(sum(value * value for value in vector))
                if not math.isfinite(norm) or norm <= 0:
                    raise ValueError("Embedding 向量範數無效")
                vector_blob = array("f", vector).tobytes()
                await session.execute(
                    sqlite_insert(SummaryEmbeddingRecord)
                    .values(
                        summary_id=summary_id,
                        chunk_index=chunk_index,
                        chunk_text=chunk,
                        source_text_sha256=hashlib.sha256(
                            chunk.encode("utf-8")
                        ).hexdigest(),
                        model_name=model_name,
                        dimension=dimension,
                        vector_blob=vector_blob,
                        vector_norm=norm,
                        created_at=effective_now,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            "summary_id",
                            "chunk_index",
                            "model_name",
                            "dimension",
                        ]
                    )
                )

    async def search(
        self,
        query_vector: tuple[float, ...],
        *,
        guild_id: str,
        channel_id: str | None = None,
        channel_ids: tuple[str, ...] | None = None,
        model_name: str,
        dimension: int,
        limit: int,
        exclude_segment_id: int | None = None,
    ) -> tuple[HistoricalSummary, ...]:
        """只在相同 guild 的可見頻道搜尋，且每個段落最多回傳一次。"""

        if len(query_vector) != dimension:
            raise ValueError("查詢向量維度與設定不符")
        if limit <= 0:
            return ()
        visible_channel_ids = channel_ids or ((channel_id,) if channel_id is not None else ())
        if not visible_channel_ids:
            return ()
        query_norm = math.sqrt(sum(value * value for value in query_vector))
        if not math.isfinite(query_norm) or query_norm <= 0:
            raise ValueError("查詢向量範數無效")

        conditions = [
            ConversationSegmentRecord.guild_id == guild_id,
            ConversationSegmentRecord.channel_id.in_(visible_channel_ids),
            SummaryEmbeddingRecord.model_name == model_name,
            SummaryEmbeddingRecord.dimension == dimension,
        ]
        if exclude_segment_id is not None:
            conditions.append(ConversationSegmentRecord.id != exclude_segment_id)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        SummaryEmbeddingRecord.vector_blob,
                        SummaryEmbeddingRecord.vector_norm,
                        SegmentSummaryRecord.id,
                        SegmentSummaryRecord.segment_id,
                        SegmentSummaryRecord.content,
                        ConversationSegmentRecord.channel_id,
                    )
                    .join(
                        SegmentSummaryRecord,
                        SegmentSummaryRecord.id == SummaryEmbeddingRecord.summary_id,
                    )
                    .join(
                        ConversationSegmentRecord,
                        ConversationSegmentRecord.id == SegmentSummaryRecord.segment_id,
                    )
                    .where(*conditions)
                )
            ).all()

        best_by_segment: dict[int, HistoricalSummary] = {}
        for blob, vector_norm, summary_id, segment_id, content, source_channel_id in rows:
            candidate = array("f")
            candidate.frombytes(blob)
            if len(candidate) != dimension:
                continue
            dot = sum(left * right for left, right in zip(query_vector, candidate, strict=True))
            score = dot / (query_norm * vector_norm)
            current = best_by_segment.get(segment_id)
            if current is None or score > current.score:
                best_by_segment[segment_id] = HistoricalSummary(
                    summary_id=summary_id,
                    segment_id=segment_id,
                    channel_id=source_channel_id,
                    content=content,
                    score=score,
                )
        matches = tuple(
            sorted(best_by_segment.values(), key=lambda item: item.score, reverse=True)[:limit]
        )
        if not matches:
            return ()

        # 摘要只負責找出相關段落；帶回非敏感原始發言，避免摘要改寫時
        # 遺失作者與原話的對應。Discord user ID 僅在本機用來區分作者。
        segment_ids = tuple(item.segment_id for item in matches)
        async with self._session_factory() as session:
            source_rows = (
                await session.execute(
                    select(
                        MessageRecord.segment_id,
                        MessageRecord.author_id,
                        MessageRecord.author_display_name,
                        MessageRecord.content,
                        MessageRecord.is_bot,
                    )
                    .where(
                        MessageRecord.segment_id.in_(segment_ids),
                        MessageRecord.is_sensitive.is_(False),
                    )
                    .order_by(
                        MessageRecord.segment_id,
                        MessageRecord.discord_created_at,
                        MessageRecord.id,
                    )
                )
            ).all()

        source_by_segment: dict[int, list[tuple[str, str | None, str, bool]]] = {}
        for segment_id, author_id, display_name, content, is_bot in source_rows:
            source_by_segment.setdefault(segment_id, []).append(
                (author_id, display_name, content, is_bot)
            )

        enriched: list[HistoricalSummary] = []
        for match in matches:
            speaker_labels: dict[str, str] = {}
            rendered: list[str] = []
            for author_id, display_name, content, is_bot in source_by_segment.get(
                match.segment_id, []
            ):
                label = speaker_labels.setdefault(
                    author_id, f"發言者{len(speaker_labels) + 1}"
                )
                name = (display_name or "未命名成員").strip()
                author = "Salt" if is_bot else name
                rendered.append(f"{author}（{label}）: {content.strip()}")
            enriched.append(
                HistoricalSummary(
                    summary_id=match.summary_id,
                    segment_id=match.segment_id,
                    content=match.content,
                    score=match.score,
                    channel_id=match.channel_id,
                    evidence="\n".join(rendered)[-2_400:],
                )
            )
        return tuple(enriched)

    async def has_searchable_vectors(
        self,
        *,
        guild_id: str,
        channel_id: str | None = None,
        channel_ids: tuple[str, ...] | None = None,
        model_name: str,
        dimension: int,
        exclude_segment_id: int | None = None,
    ) -> bool:
        """免費確認可見範圍內至少存在一筆向量，避免無效查詢費用。"""

        visible_channel_ids = channel_ids or ((channel_id,) if channel_id is not None else ())
        if not visible_channel_ids:
            return False

        conditions = [
            ConversationSegmentRecord.guild_id == guild_id,
            ConversationSegmentRecord.channel_id.in_(visible_channel_ids),
            SummaryEmbeddingRecord.model_name == model_name,
            SummaryEmbeddingRecord.dimension == dimension,
        ]
        if exclude_segment_id is not None:
            conditions.append(ConversationSegmentRecord.id != exclude_segment_id)
        async with self._session_factory() as session:
            vector_id = await session.scalar(
                select(SummaryEmbeddingRecord.id)
                .join(
                    SegmentSummaryRecord,
                    SegmentSummaryRecord.id == SummaryEmbeddingRecord.summary_id,
                )
                .join(
                    ConversationSegmentRecord,
                    ConversationSegmentRecord.id == SegmentSummaryRecord.segment_id,
                )
                .where(*conditions)
                .limit(1)
            )
        return vector_id is not None
