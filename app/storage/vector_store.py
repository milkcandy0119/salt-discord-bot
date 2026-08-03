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
    SegmentSummaryRecord,
    SummaryEmbeddingRecord,
)


@dataclass(frozen=True, slots=True)
class HistoricalSummary:
    """可放入聊天上下文的同頻道歷史摘要。"""

    summary_id: int
    segment_id: int
    content: str
    score: float


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
        channel_id: str,
        model_name: str,
        dimension: int,
        limit: int,
        exclude_segment_id: int | None = None,
    ) -> tuple[HistoricalSummary, ...]:
        """只在相同伺服器與頻道內搜尋，且每個段落最多回傳一次。"""

        if len(query_vector) != dimension:
            raise ValueError("查詢向量維度與設定不符")
        if limit <= 0:
            return ()
        query_norm = math.sqrt(sum(value * value for value in query_vector))
        if not math.isfinite(query_norm) or query_norm <= 0:
            raise ValueError("查詢向量範數無效")

        conditions = [
            ConversationSegmentRecord.guild_id == guild_id,
            ConversationSegmentRecord.channel_id == channel_id,
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
        for blob, vector_norm, summary_id, segment_id, content in rows:
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
                    content=content,
                    score=score,
                )
        return tuple(
            sorted(best_by_segment.values(), key=lambda item: item.score, reverse=True)[:limit]
        )

    async def has_searchable_vectors(
        self,
        *,
        guild_id: str,
        channel_id: str,
        model_name: str,
        dimension: int,
        exclude_segment_id: int | None = None,
    ) -> bool:
        """免費確認搜尋範圍內至少存在一筆向量，避免無效查詢費用。"""

        conditions = [
            ConversationSegmentRecord.guild_id == guild_id,
            ConversationSegmentRecord.channel_id == channel_id,
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
