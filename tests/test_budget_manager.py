import asyncio
from dataclasses import dataclass, field

import pytest

from app.ai.budget_manager import (
    BudgetExceededError,
    BudgetManager,
    BudgetSnapshot,
    ModelPrice,
    PaidPurpose,
    Reservation,
    ReservationStateError,
)
from app.storage.database import Database

ONE_DOLLAR_PRICE = ModelPrice(
    model_name="fake-model",
    price_version="test-v1",
    input_microusd_per_million_tokens=1_000_000,
    output_microusd_per_million_tokens=0,
)


@dataclass
class FakeThresholdNotifier:
    notified_thresholds: list[int] = field(default_factory=list)

    async def notify_threshold(self, threshold_percent: int, snapshot: BudgetSnapshot) -> None:
        assert snapshot.global_spent_microusd >= threshold_percent * 100_000
        self.notified_thresholds.append(threshold_percent)


@dataclass
class FailOnceThresholdNotifier(FakeThresholdNotifier):
    failed: bool = False

    async def notify_threshold(self, threshold_percent: int, snapshot: BudgetSnapshot) -> None:
        if not self.failed:
            self.failed = True
            raise RuntimeError("模擬通知失敗")
        await super().notify_threshold(threshold_percent, snapshot)


def make_manager(database: Database) -> BudgetManager:
    return BudgetManager(database.session_factory)


async def reserve_dollars(
    manager: BudgetManager,
    dollars: int,
    *,
    purpose: PaidPurpose = PaidPurpose.FOREGROUND_CHAT,
) -> Reservation:
    return await manager.reserve(
        purpose=purpose,
        price=ONE_DOLLAR_PRICE,
        maximum_input_tokens=dollars * 1_000_000,
        maximum_output_tokens=0,
    )


def test_price_quote_uses_exact_integer_microusd_with_ceiling() -> None:
    price = ModelPrice(
        model_name="fake-model",
        price_version="test-v2",
        input_microusd_per_million_tokens=1_500_000,
        output_microusd_per_million_tokens=2_000_000,
    )

    assert price.quote(input_tokens=1, output_tokens=1) == 4
    assert isinstance(price.quote(input_tokens=1, output_tokens=1), int)


@pytest.mark.asyncio
async def test_concurrent_reservations_never_exceed_global_ten_dollars(
    database: Database,
) -> None:
    manager = make_manager(database)

    results = await asyncio.gather(
        *(reserve_dollars(manager, 1) for _ in range(12)),
        return_exceptions=True,
    )
    reservations = [result for result in results if isinstance(result, Reservation)]
    rejected = [result for result in results if isinstance(result, BudgetExceededError)]
    snapshot = await manager.get_snapshot()

    assert len(reservations) == 10
    assert len(rejected) == 2
    assert snapshot.global_reserved_microusd == 10_000_000
    assert snapshot.global_spent_microusd == 0


@pytest.mark.asyncio
async def test_background_reservations_cannot_exceed_three_dollars(
    database: Database,
) -> None:
    manager = make_manager(database)

    results = await asyncio.gather(
        *(
            reserve_dollars(manager, 1, purpose=PaidPurpose.SUMMARY)
            for _ in range(4)
        ),
        return_exceptions=True,
    )
    reservations = [result for result in results if isinstance(result, Reservation)]
    rejected = [result for result in results if isinstance(result, BudgetExceededError)]
    snapshot = await manager.get_snapshot()

    assert len(reservations) == 3
    assert len(rejected) == 1
    assert rejected[0].limit_name == "background"
    assert snapshot.background_reserved_microusd == 3_000_000
    assert snapshot.global_reserved_microusd == 3_000_000


@pytest.mark.asyncio
async def test_background_settlement_updates_both_ledgers(
    database: Database,
) -> None:
    manager = make_manager(database)
    reservation = await reserve_dollars(manager, 2, purpose=PaidPurpose.EMBEDDING)

    await manager.settle(
        reservation.reservation_id,
        input_tokens=1_000_000,
        output_tokens=0,
    )
    snapshot = await manager.get_snapshot()

    assert snapshot.global_spent_microusd == 1_000_000
    assert snapshot.background_spent_microusd == 1_000_000
    assert snapshot.global_reserved_microusd == 0
    assert snapshot.background_reserved_microusd == 0


@pytest.mark.asyncio
async def test_foreground_chat_can_use_the_entire_global_budget(
    database: Database,
) -> None:
    manager = make_manager(database)

    await reserve_dollars(manager, 10)
    snapshot = await manager.get_snapshot()

    assert snapshot.global_reserved_microusd == 10_000_000
    assert snapshot.background_reserved_microusd == 0


@pytest.mark.asyncio
async def test_settlement_releases_difference_and_records_actual_tokens(
    database: Database,
) -> None:
    manager = make_manager(database)
    reservation = await reserve_dollars(manager, 1)

    settlement = await manager.settle(
        reservation.reservation_id,
        input_tokens=500_000,
        output_tokens=0,
    )
    snapshot = await manager.get_snapshot()
    call = await manager.get_call(reservation.reservation_id)

    assert settlement.actual_cost_microusd == 500_000
    assert snapshot.global_spent_microusd == 500_000
    assert snapshot.global_reserved_microusd == 0
    assert call is not None
    assert call.status == "settled"
    assert call.input_tokens == 500_000
    assert call.model_name == "fake-model"
    assert call.price_version == "test-v1"


@pytest.mark.asyncio
async def test_releasing_known_unbilled_failure_returns_reserved_budget(
    database: Database,
) -> None:
    manager = make_manager(database)
    reservation = await reserve_dollars(manager, 1)

    released = await manager.release_unbilled(
        reservation.reservation_id,
        error_code="request_not_sent",
    )
    snapshot = await manager.get_snapshot()
    call = await manager.get_call(reservation.reservation_id)

    assert released is True
    assert snapshot.global_reserved_microusd == 0
    assert snapshot.global_spent_microusd == 0
    assert call is not None and call.status == "released_unbilled"


@pytest.mark.asyncio
async def test_unknown_usage_keeps_reservation_and_never_assumes_zero_cost(
    database: Database,
) -> None:
    manager = make_manager(database)
    reservation = await reserve_dollars(manager, 10)

    changed = await manager.mark_usage_uncertain(
        reservation.reservation_id,
        error_code="api_timeout",
    )
    snapshot = await manager.get_snapshot()
    call = await manager.get_call(reservation.reservation_id)

    assert changed is True
    assert snapshot.global_reserved_microusd == 10_000_000
    assert call is not None and call.status == "usage_uncertain"
    with pytest.raises(BudgetExceededError):
        await reserve_dollars(manager, 1)


@pytest.mark.asyncio
async def test_settlement_is_idempotent_and_cannot_be_changed_afterward(
    database: Database,
) -> None:
    manager = make_manager(database)
    reservation = await reserve_dollars(manager, 1)

    first = await manager.settle(
        reservation.reservation_id,
        input_tokens=500_000,
        output_tokens=0,
    )
    second = await manager.settle(
        reservation.reservation_id,
        input_tokens=500_000,
        output_tokens=0,
    )

    assert second == first
    assert (await manager.get_snapshot()).global_spent_microusd == 500_000
    with pytest.raises(ReservationStateError):
        await manager.settle(
            reservation.reservation_id,
            input_tokens=600_000,
            output_tokens=0,
        )


@pytest.mark.asyncio
async def test_actual_usage_above_reservation_is_recorded_conservatively(
    database: Database,
) -> None:
    manager = make_manager(database)
    reservation = await reserve_dollars(manager, 1)

    settlement = await manager.settle(
        reservation.reservation_id,
        input_tokens=1_100_000,
        output_tokens=0,
    )
    call = await manager.get_call(reservation.reservation_id)

    assert settlement.actual_cost_microusd == 1_100_000
    assert call is not None and call.status == "settled_over_reservation"
    assert (await manager.get_snapshot()).global_spent_microusd == 1_100_000


@pytest.mark.asyncio
async def test_budget_and_reservations_persist_across_manager_restart(
    database: Database,
) -> None:
    first_manager = make_manager(database)
    reservation = await reserve_dollars(first_manager, 2)
    await first_manager.settle(
        reservation.reservation_id,
        input_tokens=1_000_000,
        output_tokens=0,
    )

    restarted_manager = make_manager(database)
    snapshot = await restarted_manager.get_snapshot()

    assert snapshot.global_spent_microusd == 1_000_000
    assert snapshot.global_reserved_microusd == 0


@pytest.mark.asyncio
async def test_seventy_and_ninety_percent_notifications_are_each_sent_once(
    database: Database,
) -> None:
    manager = make_manager(database)
    notifier = FakeThresholdNotifier()
    first = await reserve_dollars(manager, 7)
    await manager.settle(
        first.reservation_id,
        input_tokens=7_000_000,
        output_tokens=0,
    )

    assert await manager.dispatch_pending_notifications(notifier) == (70,)
    assert await manager.dispatch_pending_notifications(notifier) == ()

    second = await reserve_dollars(manager, 2)
    await manager.settle(
        second.reservation_id,
        input_tokens=2_000_000,
        output_tokens=0,
    )

    assert await manager.dispatch_pending_notifications(notifier) == (90,)
    assert await manager.dispatch_pending_notifications(notifier) == ()
    assert notifier.notified_thresholds == [70, 90]
    assert await manager.get_notification_statuses() == {70: "sent", 90: "sent"}


@pytest.mark.asyncio
async def test_failed_threshold_notification_returns_to_pending_for_retry(
    database: Database,
) -> None:
    manager = make_manager(database)
    notifier = FailOnceThresholdNotifier()
    reservation = await reserve_dollars(manager, 7)
    await manager.settle(
        reservation.reservation_id,
        input_tokens=7_000_000,
        output_tokens=0,
    )

    with pytest.raises(RuntimeError, match="模擬通知失敗"):
        await manager.dispatch_pending_notifications(notifier)

    assert await manager.get_notification_statuses() == {70: "pending"}
    assert await manager.dispatch_pending_notifications(notifier) == (70,)
    assert notifier.notified_thresholds == [70]
