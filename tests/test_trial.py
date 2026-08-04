from datetime import UTC, datetime, timedelta

import pytest

from app.ai.budget_manager import (
    BudgetExceededError,
    BudgetManager,
    ModelPrice,
    PaidPurpose,
)
from app.storage.database import Database
from app.storage.models import MessageRecord
from app.storage.trial import TrialRepository, TrialStateError

NOW = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)


async def _start(repository: TrialRepository, **overrides: object) -> None:
    values: dict[str, object] = {
        "guild_ids": frozenset({1}),
        "channel_ids": frozenset({10, 11}),
        "companion_channel_ids": frozenset({11}),
        "timezone_name": "Asia/Taipei",
        "duration": timedelta(days=7),
        "global_increment_limit_microusd": 1_000_000,
        "background_increment_limit_microusd": 250_000,
        "companion_daily_reply_limit": 20,
        "now": NOW,
    }
    values.update(overrides)
    await repository.start(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_trial_start_snapshots_budget_and_cannot_be_reset(database: Database) -> None:
    repository = TrialRepository(database.session_factory)

    await _start(repository)
    report = await repository.report(now=NOW)

    assert report["status"] == "active"
    assert report["global_increment_microusd"] == 0
    assert report["global_increment_limit_microusd"] == 1_000_000
    assert report["guild_count"] == 1
    assert report["channel_count"] == 2
    with pytest.raises(TrialStateError, match="不得重設基準"):
        await _start(repository)


@pytest.mark.asyncio
async def test_trial_global_increment_limit_is_enforced_during_reservation(
    database: Database,
) -> None:
    repository = TrialRepository(database.session_factory)
    manager = BudgetManager(database.session_factory)
    await _start(repository)
    price = ModelPrice(
        model_name="test",
        price_version="test",
        input_microusd_per_million_tokens=600_000,
        output_microusd_per_million_tokens=0,
    )

    await manager.reserve(
        purpose=PaidPurpose.FOREGROUND_CHAT,
        price=price,
        maximum_input_tokens=1_000_000,
        maximum_output_tokens=0,
    )
    with pytest.raises(BudgetExceededError) as error:
        await manager.reserve(
            purpose=PaidPurpose.FOREGROUND_CHAT,
            price=price,
            maximum_input_tokens=1_000_000,
            maximum_output_tokens=0,
        )

    assert error.value.limit_name == "trial_global"


@pytest.mark.asyncio
async def test_trial_background_increment_limit_is_independent(database: Database) -> None:
    repository = TrialRepository(database.session_factory)
    manager = BudgetManager(database.session_factory)
    await _start(repository)
    price = ModelPrice(
        model_name="test",
        price_version="test",
        input_microusd_per_million_tokens=300_000,
        output_microusd_per_million_tokens=0,
    )

    with pytest.raises(BudgetExceededError) as error:
        await manager.reserve(
            purpose=PaidPurpose.SUMMARY,
            price=price,
            maximum_input_tokens=1_000_000,
            maximum_output_tokens=0,
        )

    assert error.value.limit_name == "trial_background"


@pytest.mark.asyncio
async def test_pause_finish_and_expiry_block_new_paid_calls(database: Database) -> None:
    repository = TrialRepository(database.session_factory)
    manager = BudgetManager(database.session_factory)
    await _start(repository)
    price = ModelPrice("test", "test", 1, 0)

    await repository.set_status("pause", now=NOW + timedelta(hours=1))
    with pytest.raises(BudgetExceededError) as paused:
        await manager.reserve(
            purpose=PaidPurpose.FOREGROUND_CHAT,
            price=price,
            maximum_input_tokens=1_000_000,
            maximum_output_tokens=0,
        )
    assert paused.value.limit_name == "trial_inactive"

    await repository.set_status("resume", now=NOW + timedelta(hours=2))
    await repository.set_status("finish", now=NOW + timedelta(hours=3))
    with pytest.raises(BudgetExceededError) as finished:
        await manager.reserve(
            purpose=PaidPurpose.FOREGROUND_CHAT,
            price=price,
            maximum_input_tokens=1_000_000,
            maximum_output_tokens=0,
        )
    assert finished.value.limit_name == "trial_inactive"


@pytest.mark.asyncio
async def test_companion_daily_limit_is_idempotent_and_uses_trial_timezone(
    database: Database,
) -> None:
    repository = TrialRepository(database.session_factory)
    await _start(repository, companion_daily_reply_limit=2)

    first = await repository.reserve_companion_reply(
        guild_id="1", channel_id="11", message_id="100", now=NOW
    )
    replay = await repository.reserve_companion_reply(
        guild_id="1", channel_id="11", message_id="100", now=NOW
    )
    second = await repository.reserve_companion_reply(
        guild_id="1", channel_id="11", message_id="101", now=NOW
    )
    blocked = await repository.reserve_companion_reply(
        guild_id="1", channel_id="11", message_id="102", now=NOW
    )

    assert (first, replay, second, blocked) == (
        "allowed",
        "allowed",
        "allowed",
        "daily_limit",
    )
    report = await repository.report(now=NOW)
    assert report["companion_daily_counts"] == {"2026-08-04": 2}


@pytest.mark.asyncio
async def test_feedback_and_report_never_copy_message_content(database: Database) -> None:
    repository = TrialRepository(database.session_factory)
    await _start(repository)
    secret_marker = "這段完整聊天內容不應出現在試跑報告"
    async with database.session_factory() as session, session.begin():
        session.add(
            MessageRecord(
                discord_message_id="123456789",
                guild_id="1",
                channel_id="11",
                author_id="20",
                author_display_name="測試成員",
                content=secret_marker,
                discord_created_at=NOW + timedelta(minutes=1),
                received_at=NOW + timedelta(minutes=1),
                replied_to_message_id=None,
                is_bot=False,
                is_sensitive=False,
                sensitive_categories=[],
                processing_status="stored",
                author_notification_status="not_required",
                admin_notification_status="not_required",
            )
        )

    created = await repository.add_feedback(
        guild_id="1",
        actor_user_id="9",
        target_message_id="123456789",
        category="too_formal",
        now=NOW + timedelta(minutes=2),
    )
    duplicate = await repository.add_feedback(
        guild_id="1",
        actor_user_id="9",
        target_message_id="123456789",
        category="too_formal",
        now=NOW + timedelta(minutes=3),
    )
    await repository.record_event(
        idempotency_key="reply_result:123456789",
        event_type="reply_result",
        guild_id="1",
        channel_id="11",
        message_id="123456789",
        channel_mode="companion",
        trigger_kind="companion",
        reason="discord_reply_saved",
        outcome="generated",
        latency_ms=500,
        now=NOW + timedelta(minutes=2),
    )
    report = await repository.report(now=NOW + timedelta(minutes=3))

    assert created == "created"
    assert duplicate == "duplicate"
    assert report["feedback_counts"] == {"too_formal": 1}
    assert report["reply_latency_ms"] == {"count": 1, "p50": 500, "p95": 500}
    assert secret_marker not in str(report)
    assert "測試成員" not in str(report)
