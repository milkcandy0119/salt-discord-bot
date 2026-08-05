"""只在確定要回覆時執行的同頻道歷史摘要檢索。"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.ai.background_errors import (
    BackgroundBudgetDeferred,
    PermanentBackgroundError,
    RetryableBackgroundError,
)
from app.ai.embedding_service import EmbeddingService
from app.storage.memory_groups import ChannelAccessRepository
from app.storage.models import MessageRecord
from app.storage.vector_store import HistoricalSummary, SQLiteVectorStore

LOGGER = logging.getLogger(__name__)


class HistoricalContextRetriever:
    """付費建立查詢向量後，精確搜尋同頻道的歷史摘要。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        embedding_service: EmbeddingService,
        vector_store: SQLiteVectorStore,
        model_name: str,
        dimensions: int,
        result_limit: int,
        access_repository: ChannelAccessRepository | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._embedding_service = embedding_service
        self._vector_store = vector_store
        self._model_name = model_name
        self._dimensions = dimensions
        self._result_limit = result_limit
        self._access_repository = access_repository

    async def retrieve(
        self,
        *,
        trigger_message_id: str,
        query_text: str,
    ) -> tuple[HistoricalSummary, ...]:
        """失敗或額度不足時安靜略過，不影響一般聊天回覆。"""

        async with self._session_factory() as session:
            trigger = await session.scalar(
                select(MessageRecord).where(
                    MessageRecord.discord_message_id == trigger_message_id
                )
            )
        if trigger is None or trigger.segment_id is None or not query_text.strip():
            return ()
        visible_channel_ids = (
            await self._access_repository.visible_channel_ids(
                guild_id=trigger.guild_id, channel_id=trigger.channel_id
            )
            if self._access_repository is not None
            else (trigger.channel_id,)
        )
        if not await self._vector_store.has_searchable_vectors(
            guild_id=trigger.guild_id,
            channel_ids=visible_channel_ids,
            model_name=self._model_name,
            dimension=self._dimensions,
            exclude_segment_id=trigger.segment_id,
        ):
            return ()
        try:
            query_vector = await self._embedding_service.embed_query(query_text)
            return await self._vector_store.search(
                query_vector,
                guild_id=trigger.guild_id,
                channel_ids=visible_channel_ids,
                model_name=self._model_name,
                dimension=self._dimensions,
                limit=self._result_limit,
                exclude_segment_id=trigger.segment_id,
            )
        except (
            BackgroundBudgetDeferred,
            RetryableBackgroundError,
            PermanentBackgroundError,
            ValueError,
        ) as error:
            LOGGER.info("歷史摘要檢索略過 reason=%s", type(error).__name__)
            return ()
