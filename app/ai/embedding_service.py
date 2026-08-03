"""摘要分塊與查詢文字的 OpenAI Embedding 服務。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import openai

from app.ai.background_errors import (
    BackgroundBudgetDeferred,
    PermanentBackgroundError,
    RetryableBackgroundError,
)
from app.ai.budget_manager import BudgetExceededError, BudgetManager, ModelPrice, PaidPurpose
from app.ai.chat_service import ProviderCallError
from app.security.sensitive_filter import SensitiveFilter
from app.storage.background_memory import BackgroundJob, BackgroundMemoryRepository
from app.storage.vector_store import SQLiteVectorStore


@dataclass(frozen=True, slots=True)
class ProviderEmbeddingResponse:
    """Embedding 供應商回應的必要欄位。"""

    vectors: tuple[tuple[float, ...], ...]
    input_tokens: int | None


class EmbeddingProvider(Protocol):
    """可使用假物件測試的向量介面。"""

    async def embed(
        self,
        *,
        model: str,
        texts: tuple[str, ...],
        dimensions: int,
    ) -> ProviderEmbeddingResponse: ...


class OpenAIEmbeddingProvider:
    """OpenAI Embeddings API 的非同步配接器。"""

    def __init__(self, api_key: str) -> None:
        self._client = openai.AsyncOpenAI(api_key=api_key, max_retries=0, timeout=30.0)

    async def embed(
        self,
        *,
        model: str,
        texts: tuple[str, ...],
        dimensions: int,
    ) -> ProviderEmbeddingResponse:
        """以浮點格式取得指定維度的向量。"""

        try:
            response = await self._client.embeddings.create(
                model=model,
                input=list(texts),
                dimensions=dimensions,
                encoding_format="float",
            )
        except (openai.APIConnectionError, openai.APIStatusError) as error:
            raise ProviderCallError(
                "openai_usage_uncertain", usage_may_be_billed=True
            ) from error
        except (TypeError, ValueError) as error:
            raise ProviderCallError("request_not_sent", usage_may_be_billed=False) from error
        ordered = sorted(response.data, key=lambda item: item.index)
        usage = response.usage
        return ProviderEmbeddingResponse(
            vectors=tuple(tuple(item.embedding) for item in ordered),
            input_tokens=usage.prompt_tokens if usage is not None else None,
        )


class EmbeddingService:
    """受背景預算約束的摘要與聊天查詢向量化服務。"""

    def __init__(
        self,
        *,
        provider: EmbeddingProvider | None,
        repository: BackgroundMemoryRepository,
        vector_store: SQLiteVectorStore,
        budget_manager: BudgetManager,
        price: ModelPrice,
        sensitive_filter: SensitiveFilter,
        dimensions: int,
        chunk_characters: int,
        chunk_overlap_characters: int,
    ) -> None:
        if chunk_characters <= 0:
            raise ValueError("摘要分塊字元數必須大於零")
        if not 0 <= chunk_overlap_characters < chunk_characters:
            raise ValueError("摘要分塊重疊量必須小於分塊大小")
        self._provider = provider
        self._repository = repository
        self._vector_store = vector_store
        self._budget_manager = budget_manager
        self._price = price
        self._sensitive_filter = sensitive_filter
        self._dimensions = dimensions
        self._chunk_characters = chunk_characters
        self._chunk_overlap_characters = chunk_overlap_characters

    async def process(self, job: BackgroundJob) -> None:
        """冪等地分塊並向量化一筆已保存摘要。"""

        if job.summary_id is None:
            raise PermanentBackgroundError("missing_summary_id")
        if await self._repository.embeddings_exist(
            summary_id=job.summary_id,
            model_name=self._price.model_name,
            dimension=self._dimensions,
        ):
            return
        summary = await self._repository.get_summary(job.summary_id)
        if summary is None:
            raise PermanentBackgroundError("summary_not_found")
        chunks = self.chunk(summary.content)
        vectors = await self.embed_texts(chunks)
        try:
            await self._vector_store.store(
                summary_id=summary.id,
                chunks=chunks,
                vectors=vectors,
                model_name=self._price.model_name,
                dimension=self._dimensions,
            )
        except ValueError as error:
            raise PermanentBackgroundError("invalid_embedding_vector") from error

    async def embed_query(self, text: str) -> tuple[float, ...]:
        """只在已決定要聊天時，付費建立一個歷史檢索查詢向量。"""

        vectors = await self.embed_texts((text.strip(),))
        return vectors[0]

    async def embed_texts(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """在安全掃描與預算預留後呼叫一次 Embeddings API。"""

        if self._provider is None:
            raise BackgroundBudgetDeferred("openai_not_configured")
        if not texts or any(
            not text or self._sensitive_filter.scan(text).is_sensitive for text in texts
        ):
            raise PermanentBackgroundError("blocked_embedding_input")
        try:
            reservation = await self._budget_manager.reserve(
                purpose=PaidPurpose.EMBEDDING,
                price=self._price,
                maximum_input_tokens=(
                    sum(len(text.encode("utf-8")) for text in texts) + 32 * len(texts)
                ),
                maximum_output_tokens=0,
            )
        except BudgetExceededError as error:
            raise BackgroundBudgetDeferred(error.limit_name) from error
        try:
            response = await self._provider.embed(
                model=self._price.model_name,
                texts=texts,
                dimensions=self._dimensions,
            )
        except ProviderCallError as error:
            if error.usage_may_be_billed:
                await self._budget_manager.mark_usage_uncertain(
                    reservation.reservation_id, error_code=error.error_code
                )
                raise PermanentBackgroundError(error.error_code) from error
            await self._budget_manager.release_unbilled(
                reservation.reservation_id, error_code=error.error_code
            )
            raise RetryableBackgroundError(error.error_code) from error
        except Exception as error:
            await self._budget_manager.mark_usage_uncertain(
                reservation.reservation_id,
                error_code="unexpected_provider_error",
            )
            raise PermanentBackgroundError("unexpected_provider_error") from error
        if response.input_tokens is None:
            await self._budget_manager.mark_usage_uncertain(
                reservation.reservation_id, error_code="missing_usage"
            )
            raise PermanentBackgroundError("missing_usage")
        await self._budget_manager.settle(
            reservation.reservation_id,
            input_tokens=response.input_tokens,
            output_tokens=0,
        )
        if len(response.vectors) != len(texts):
            raise PermanentBackgroundError("embedding_count_mismatch")
        return response.vectors

    def chunk(self, text: str) -> tuple[str, ...]:
        """以固定字元窗和重疊量建立可重現的摘要分塊。"""

        cleaned = text.strip()
        if not cleaned:
            raise PermanentBackgroundError("empty_summary")
        step = self._chunk_characters - self._chunk_overlap_characters
        return tuple(
            cleaned[start : start + self._chunk_characters]
            for start in range(0, len(cleaned), step)
        )
