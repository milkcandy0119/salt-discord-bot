"""不呼叫 AI、可重啟恢復且不公開補發的提醒派送器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from app.storage.reminders import Reminder, ReminderRepository


class ReminderDeliveryError(RuntimeError):
    """私訊提醒失敗，並標示是否適合稍後重試。"""

    def __init__(self, error_code: str, *, retryable: bool) -> None:
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(error_code)


class ReminderSender(Protocol):
    """可由假物件取代的私訊發送介面。"""

    async def send(self, reminder: Reminder) -> None: ...


@dataclass(frozen=True, slots=True)
class ReminderDispatchResult:
    """單批提醒派送的不含內容統計。"""

    sent: int
    retried: int
    failed: int


class ReminderDispatcher:
    """最舊到期優先派送，失敗時保留資料且永不公開補發。"""

    def __init__(
        self,
        *,
        repository: ReminderRepository,
        sender: ReminderSender,
        stale_after: timedelta,
        retry_base_delay: timedelta,
        maximum_per_run: int,
    ) -> None:
        self._repository = repository
        self._sender = sender
        self._stale_after = stale_after
        self._retry_base_delay = retry_base_delay
        self._maximum_per_run = maximum_per_run

    async def run_once(self) -> ReminderDispatchResult:
        """派送有限批次，單筆失敗不阻塞後續到期提醒。"""

        sent = retried = failed = 0
        for _ in range(self._maximum_per_run):
            reminder = await self._repository.claim_due(stale_after=self._stale_after)
            if reminder is None:
                break
            try:
                await self._sender.send(reminder)
            except ReminderDeliveryError as error:
                status = await self._repository.retry_or_fail(
                    reminder,
                    error_code=error.error_code,
                    retryable=error.retryable,
                    base_delay=self._retry_base_delay,
                )
                retried += int(status == "retry_wait")
                failed += int(status == "failed")
            except Exception:
                status = await self._repository.retry_or_fail(
                    reminder,
                    error_code="unexpected_delivery_error",
                    retryable=True,
                    base_delay=self._retry_base_delay,
                )
                retried += int(status == "retry_wait")
                failed += int(status == "failed")
            else:
                await self._repository.mark_sent(reminder.id)
                sent += 1
        return ReminderDispatchResult(sent, retried, failed)
