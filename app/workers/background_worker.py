"""持久化摘要與向量工作的排程執行器。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from app.ai.background_errors import (
    BackgroundBudgetDeferred,
    PermanentBackgroundError,
    RetryableBackgroundError,
)
from app.ai.embedding_service import EmbeddingService
from app.ai.summary_service import SummaryService
from app.storage.background_memory import BackgroundMemoryRepository

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerRunResult:
    """單次排程執行的不含內容統計。"""

    completed: int
    deferred: int
    retried: int
    failed: int


class BackgroundWorker:
    """最舊優先處理工作，並隔離壞工作避免堵塞佇列。"""

    def __init__(
        self,
        *,
        repository: BackgroundMemoryRepository,
        summary_service: SummaryService,
        embedding_service: EmbeddingService,
        stale_after: timedelta,
        retry_base_delay: timedelta,
        budget_retry_after: timedelta,
        maximum_jobs_per_run: int,
    ) -> None:
        self._repository = repository
        self._summary_service = summary_service
        self._embedding_service = embedding_service
        self._stale_after = stale_after
        self._retry_base_delay = retry_base_delay
        self._budget_retry_after = budget_retry_after
        self._maximum_jobs_per_run = maximum_jobs_per_run

    async def run_once(self) -> WorkerRunResult:
        """執行有上限的一批工作；額度不足時立刻停止本批次。"""

        completed = deferred = retried = failed = 0
        for _ in range(self._maximum_jobs_per_run):
            job = await self._repository.claim_oldest(stale_after=self._stale_after)
            if job is None:
                break
            try:
                if job.job_type == "summarize_segment":
                    await self._summary_service.process(job)
                elif job.job_type == "embed_summary":
                    await self._embedding_service.process(job)
                else:
                    raise PermanentBackgroundError("unknown_job_type")
            except BackgroundBudgetDeferred:
                await self._repository.defer_for_budget(
                    job.id, retry_after=self._budget_retry_after
                )
                deferred += 1
                break
            except RetryableBackgroundError as error:
                status = await self._repository.retry_or_fail(
                    job,
                    error_code=error.error_code,
                    base_delay=self._retry_base_delay,
                )
                retried += int(status == "retry_wait")
                failed += int(status == "failed")
            except PermanentBackgroundError as error:
                await self._repository.mark_failed(job.id, error_code=error.error_code)
                failed += 1
            except Exception as error:
                status = await self._repository.retry_or_fail(
                    job,
                    error_code="unexpected_worker_error",
                    base_delay=self._retry_base_delay,
                )
                retried += int(status == "retry_wait")
                failed += int(status == "failed")
                LOGGER.error(
                    "背景工作發生未預期錯誤 job_id=%s error_type=%s",
                    job.id,
                    type(error).__name__,
                )
            else:
                await self._repository.mark_completed(job.id)
                completed += 1
        return WorkerRunResult(completed, deferred, retried, failed)
