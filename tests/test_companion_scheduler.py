import asyncio
from datetime import timedelta

import pytest

from app.bot.companion_scheduler import CompanionScheduler


@pytest.mark.asyncio
async def test_new_message_resets_observation_window_and_only_latest_callback_runs() -> None:
    scheduler = CompanionScheduler(observation_window=timedelta(milliseconds=30))
    calls: list[str] = []

    async def first() -> None:
        calls.append("first")

    async def second() -> None:
        calls.append("second")

    scheduler.schedule(20, first)
    await asyncio.sleep(0.01)
    scheduler.schedule(20, second)
    await asyncio.sleep(0.05)
    await scheduler.close()

    assert calls == ["second"]


@pytest.mark.asyncio
async def test_channels_have_independent_observation_windows() -> None:
    scheduler = CompanionScheduler(observation_window=timedelta(milliseconds=10))
    calls: list[int] = []

    async def channel_one() -> None:
        calls.append(1)

    async def channel_two() -> None:
        calls.append(2)

    scheduler.schedule(10, channel_one)
    scheduler.schedule(20, channel_two)
    await asyncio.sleep(0.03)
    await scheduler.close()

    assert sorted(calls) == [1, 2]
