"""陪伴模式每個頻道各自獨立的安靜觀察排程器。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta


class CompanionScheduler:
    """新訊息會取消同頻道舊計時，安靜滿設定時間才執行評估。"""

    def __init__(self, *, observation_window: timedelta) -> None:
        if observation_window <= timedelta(0):
            raise ValueError("陪伴模式觀察窗必須大於零")
        self._delay_seconds = observation_window.total_seconds()
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def schedule(
        self,
        channel_id: int,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """重設指定頻道的觀察窗並保留最新回呼。"""

        self.cancel(channel_id)
        task = asyncio.create_task(self._run(channel_id, callback))
        self._tasks[channel_id] = task

    def cancel(self, channel_id: int) -> None:
        """取消指定頻道尚未到期的觀察工作。"""

        task = self._tasks.pop(channel_id, None)
        if task is not None:
            task.cancel()

    async def close(self) -> None:
        """取消並收回所有尚未完成的觀察工作。"""

        tasks = tuple(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(
        self,
        channel_id: int,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            await asyncio.sleep(self._delay_seconds)
            await callback()
        except asyncio.CancelledError:
            return
        finally:
            current = asyncio.current_task()
            if self._tasks.get(channel_id) is current:
                self._tasks.pop(channel_id, None)
