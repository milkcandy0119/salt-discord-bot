"""階段 9 試跑生命週期、免費觀測、每日上限與報告資料存取。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import (
    BudgetStateRecord,
    MessageRecord,
    PaidAiCallRecord,
    TrialDailyCounterRecord,
    TrialEventRecord,
    TrialFeedbackRecord,
    TrialSessionRecord,
)

TRIAL_FEEDBACK_CATEGORIES = frozenset(
    {"good", "too_formal", "wrong_memory", "unwanted_reply", "missed_reply", "other"}
)


class TrialStateError(RuntimeError):
    """試跑生命週期或範圍不允許目前操作。"""


@dataclass(frozen=True, slots=True)
class TrialSession:
    """不依賴已關閉 ORM session 的試跑設定快照。"""

    id: int
    status: str
    guild_ids: frozenset[str]
    channel_ids: frozenset[str]
    companion_channel_ids: frozenset[str]
    timezone_name: str
    baseline_global_committed_microusd: int
    baseline_background_committed_microusd: int
    global_increment_limit_microusd: int
    background_increment_limit_microusd: int
    companion_daily_reply_limit: int
    started_at: datetime
    ends_at: datetime
    ended_at: datetime | None
    stopped_reason: str | None


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _snapshot(record: TrialSessionRecord) -> TrialSession:
    return TrialSession(
        id=record.id,
        status=record.status,
        guild_ids=frozenset(record.guild_ids),
        channel_ids=frozenset(record.channel_ids),
        companion_channel_ids=frozenset(record.companion_channel_ids),
        timezone_name=record.timezone_name,
        baseline_global_committed_microusd=record.baseline_global_committed_microusd,
        baseline_background_committed_microusd=(
            record.baseline_background_committed_microusd
        ),
        global_increment_limit_microusd=record.global_increment_limit_microusd,
        background_increment_limit_microusd=(
            record.background_increment_limit_microusd
        ),
        companion_daily_reply_limit=record.companion_daily_reply_limit,
        started_at=_as_utc(record.started_at),
        ends_at=_as_utc(record.ends_at),
        ended_at=_as_utc(record.ended_at) if record.ended_at else None,
        stopped_reason=record.stopped_reason,
    )


class TrialRepository:
    """試跑只保存 ID、分類、結果與彙總，不保存額外聊天內容。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def start(
        self,
        *,
        guild_ids: frozenset[int],
        channel_ids: frozenset[int],
        companion_channel_ids: frozenset[int],
        timezone_name: str,
        duration: timedelta,
        global_increment_limit_microusd: int,
        background_increment_limit_microusd: int,
        companion_daily_reply_limit: int,
        now: datetime | None = None,
    ) -> TrialSession:
        """以當下已花費加預留作為不可回退的增量預算基準。"""

        if duration <= timedelta(0):
            raise ValueError("試跑時間必須大於零")
        if not guild_ids or not channel_ids:
            raise ValueError("試跑至少需要一個伺服器與頻道")
        if not companion_channel_ids.issubset(channel_ids):
            raise ValueError("companion 頻道必須位於試跑頻道範圍")
        ZoneInfo(timezone_name)
        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            existing = await session.scalar(
                select(TrialSessionRecord.id).order_by(TrialSessionRecord.id.desc()).limit(1)
            )
            if existing is not None:
                raise TrialStateError("已存在試跑紀錄，不得重設基準規避增量上限")
            budget = await session.get(BudgetStateRecord, 1)
            if budget is None:
                raise TrialStateError("找不到預算狀態，請先執行 migration")
            record = TrialSessionRecord(
                status="active",
                guild_ids=[str(value) for value in sorted(guild_ids)],
                channel_ids=[str(value) for value in sorted(channel_ids)],
                companion_channel_ids=[
                    str(value) for value in sorted(companion_channel_ids)
                ],
                timezone_name=timezone_name,
                baseline_global_committed_microusd=(
                    budget.global_spent_microusd + budget.global_reserved_microusd
                ),
                baseline_background_committed_microusd=(
                    budget.background_spent_microusd
                    + budget.background_reserved_microusd
                ),
                global_increment_limit_microusd=global_increment_limit_microusd,
                background_increment_limit_microusd=(
                    background_increment_limit_microusd
                ),
                companion_daily_reply_limit=companion_daily_reply_limit,
                started_at=effective_now,
                ends_at=effective_now + duration,
                ended_at=None,
                stopped_reason=None,
                created_at=effective_now,
                updated_at=effective_now,
            )
            session.add(record)
            await session.flush()
            return _snapshot(record)

    async def latest(self) -> TrialSession | None:
        """讀取唯一試跑；沒有開始時回傳 None。"""

        async with self._session_factory() as session:
            record = await session.scalar(
                select(TrialSessionRecord)
                .order_by(TrialSessionRecord.id.desc())
                .limit(1)
            )
            return _snapshot(record) if record is not None else None

    async def set_status(
        self,
        status: str,
        *,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> TrialSession:
        """只允許保守的暫停、恢復或結束轉換。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            record = await session.scalar(
                select(TrialSessionRecord)
                .order_by(TrialSessionRecord.id.desc())
                .limit(1)
            )
            if record is None:
                raise TrialStateError("試跑尚未開始")
            transitions = {
                "pause": ({"active"}, "paused"),
                "resume": ({"paused"}, "active"),
                "finish": ({"active", "paused"}, "completed"),
                "stop": ({"active", "paused"}, "stopped"),
            }
            if status not in transitions:
                raise ValueError("未知的試跑狀態操作")
            allowed, target = transitions[status]
            if record.status not in allowed:
                raise TrialStateError(f"目前狀態不可執行 {status}")
            if status in {"pause", "resume"} and effective_now >= _as_utc(record.ends_at):
                raise TrialStateError("試跑已到期，只能結束並產生報告")
            record.status = target
            record.updated_at = effective_now
            if target in {"completed", "stopped"}:
                record.ended_at = effective_now
                record.stopped_reason = reason or (
                    "manual_finish" if target == "completed" else "manual_stop"
                )
            await session.flush()
            return _snapshot(record)

    async def record_event(
        self,
        *,
        idempotency_key: str,
        event_type: str,
        guild_id: str | None = None,
        channel_id: str | None = None,
        message_id: str | None = None,
        channel_mode: str | None = None,
        trigger_kind: str | None = None,
        reason: str | None = None,
        outcome: str | None = None,
        latency_ms: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """只在有效 active 試跑期間寫入不含內容的冪等事件。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            trial = await session.scalar(
                select(TrialSessionRecord)
                .where(TrialSessionRecord.status == "active")
                .order_by(TrialSessionRecord.id.desc())
                .limit(1)
            )
            if trial is None or effective_now >= _as_utc(trial.ends_at):
                return False
            if guild_id is not None and guild_id not in trial.guild_ids:
                return False
            if channel_id is not None and channel_id not in trial.channel_ids:
                return False
            result = await session.execute(
                sqlite_insert(TrialEventRecord)
                .values(
                    session_id=trial.id,
                    idempotency_key=idempotency_key[:128],
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    event_type=event_type[:32],
                    channel_mode=channel_mode,
                    trigger_kind=trigger_kind,
                    reason=reason,
                    outcome=outcome,
                    latency_ms=latency_ms,
                    created_at=effective_now,
                )
                .on_conflict_do_nothing(index_elements=["idempotency_key"])
            )
            return result.rowcount == 1

    async def reserve_companion_reply(
        self,
        *,
        guild_id: str,
        channel_id: str,
        message_id: str,
        now: datetime | None = None,
    ) -> str:
        """以條件更新原子取得今日 companion 回覆名額。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            trial = await session.scalar(
                select(TrialSessionRecord)
                .order_by(TrialSessionRecord.id.desc())
                .limit(1)
            )
            if trial is None:
                return "not_started"
            if trial.status != "active" or effective_now >= _as_utc(trial.ends_at):
                return "inactive"
            if guild_id not in trial.guild_ids or channel_id not in trial.companion_channel_ids:
                return "outside_scope"
            key = f"companion_slot:{trial.id}:{message_id}"
            existing = await session.scalar(
                select(TrialEventRecord.id).where(
                    TrialEventRecord.idempotency_key == key
                )
            )
            if existing is not None:
                return "allowed"
            local_date = effective_now.astimezone(ZoneInfo(trial.timezone_name)).date().isoformat()
            await session.execute(
                sqlite_insert(TrialDailyCounterRecord)
                .values(
                    session_id=trial.id,
                    local_date=local_date,
                    companion_reply_count=0,
                    updated_at=effective_now,
                )
                .on_conflict_do_nothing(index_elements=["session_id", "local_date"])
            )
            result = await session.execute(
                update(TrialDailyCounterRecord)
                .where(
                    TrialDailyCounterRecord.session_id == trial.id,
                    TrialDailyCounterRecord.local_date == local_date,
                    TrialDailyCounterRecord.companion_reply_count
                    < trial.companion_daily_reply_limit,
                )
                .values(
                    companion_reply_count=(
                        TrialDailyCounterRecord.companion_reply_count + 1
                    ),
                    updated_at=effective_now,
                )
            )
            if result.rowcount != 1:
                return "daily_limit"
            session.add(
                TrialEventRecord(
                    session_id=trial.id,
                    idempotency_key=key,
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    event_type="companion_slot",
                    channel_mode="companion",
                    trigger_kind="companion",
                    reason="daily_slot_reserved",
                    outcome="allowed",
                    latency_ms=None,
                    created_at=effective_now,
                )
            )
            return "allowed"

    async def add_feedback(
        self,
        *,
        guild_id: str,
        actor_user_id: str,
        target_message_id: str,
        category: str,
        now: datetime | None = None,
    ) -> str:
        """只接受試跑期間已保存訊息及固定分類。"""

        if category not in TRIAL_FEEDBACK_CATEGORIES:
            return "invalid_category"
        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            trial = await session.scalar(
                select(TrialSessionRecord)
                .order_by(TrialSessionRecord.id.desc())
                .limit(1)
            )
            if trial is None or guild_id not in trial.guild_ids:
                return "no_trial"
            feedback_end = trial.ended_at or trial.ends_at
            message = await session.scalar(
                select(MessageRecord).where(
                    MessageRecord.discord_message_id == target_message_id,
                    MessageRecord.guild_id == guild_id,
                    MessageRecord.received_at >= trial.started_at,
                    MessageRecord.received_at <= feedback_end,
                )
            )
            if message is None:
                return "message_not_found"
            result = await session.execute(
                sqlite_insert(TrialFeedbackRecord)
                .values(
                    session_id=trial.id,
                    guild_id=guild_id,
                    actor_user_id=actor_user_id,
                    target_message_id=target_message_id,
                    category=category,
                    created_at=effective_now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        "session_id",
                        "actor_user_id",
                        "target_message_id",
                        "category",
                    ]
                )
            )
            return "created" if result.rowcount == 1 else "duplicate"

    async def report(self, *, now: datetime | None = None) -> dict[str, object]:
        """產生不含訊息內容、作者名稱及模型輸出的免費彙總。"""

        effective_now = now or datetime.now(UTC)
        async with self._session_factory() as session:
            trial = await session.scalar(
                select(TrialSessionRecord)
                .order_by(TrialSessionRecord.id.desc())
                .limit(1)
            )
            if trial is None:
                return {"status": "not_started"}
            budget = await session.get(BudgetStateRecord, 1)
            if budget is None:
                raise TrialStateError("找不到預算狀態")
            event_rows = (
                await session.execute(
                    select(
                        TrialEventRecord.event_type,
                        TrialEventRecord.reason,
                        TrialEventRecord.outcome,
                        func.count(),
                    )
                    .where(TrialEventRecord.session_id == trial.id)
                    .group_by(
                        TrialEventRecord.event_type,
                        TrialEventRecord.reason,
                        TrialEventRecord.outcome,
                    )
                )
            ).all()
            feedback_rows = (
                await session.execute(
                    select(TrialFeedbackRecord.category, func.count())
                    .where(TrialFeedbackRecord.session_id == trial.id)
                    .group_by(TrialFeedbackRecord.category)
                )
            ).all()
            daily_rows = (
                await session.execute(
                    select(
                        TrialDailyCounterRecord.local_date,
                        TrialDailyCounterRecord.companion_reply_count,
                    )
                    .where(TrialDailyCounterRecord.session_id == trial.id)
                    .order_by(TrialDailyCounterRecord.local_date)
                )
            ).all()
            paid_rows = (
                await session.execute(
                    select(
                        PaidAiCallRecord.purpose,
                        func.count(),
                        func.coalesce(func.sum(PaidAiCallRecord.input_tokens), 0),
                        func.coalesce(func.sum(PaidAiCallRecord.output_tokens), 0),
                        func.coalesce(func.sum(PaidAiCallRecord.actual_cost_microusd), 0),
                    )
                    .where(PaidAiCallRecord.created_at >= trial.started_at)
                    .group_by(PaidAiCallRecord.purpose)
                )
            ).all()
            latencies = list(
                await session.scalars(
                    select(TrialEventRecord.latency_ms)
                    .where(
                        TrialEventRecord.session_id == trial.id,
                        TrialEventRecord.latency_ms.is_not(None),
                    )
                    .order_by(TrialEventRecord.latency_ms)
                )
            )
            effective_status = (
                "expired"
                if trial.status == "active" and effective_now >= _as_utc(trial.ends_at)
                else trial.status
            )
            global_committed = budget.global_spent_microusd + budget.global_reserved_microusd
            background_committed = (
                budget.background_spent_microusd + budget.background_reserved_microusd
            )
            global_increment = max(
                0, global_committed - trial.baseline_global_committed_microusd
            )
            background_increment = max(
                0,
                background_committed
                - trial.baseline_background_committed_microusd,
            )
            return {
                "session_id": trial.id,
                "status": effective_status,
                "started_at": _as_utc(trial.started_at).isoformat(),
                "ends_at": _as_utc(trial.ends_at).isoformat(),
                "guild_count": len(trial.guild_ids),
                "channel_count": len(trial.channel_ids),
                "companion_channel_count": len(trial.companion_channel_ids),
                "global_increment_microusd": global_increment,
                "global_increment_limit_microusd": trial.global_increment_limit_microusd,
                "global_remaining_microusd": max(
                    0, trial.global_increment_limit_microusd - global_increment
                ),
                "background_increment_microusd": background_increment,
                "background_increment_limit_microusd": (
                    trial.background_increment_limit_microusd
                ),
                "background_remaining_microusd": max(
                    0,
                    trial.background_increment_limit_microusd - background_increment,
                ),
                "companion_daily_reply_limit": trial.companion_daily_reply_limit,
                "companion_daily_counts": dict(daily_rows),
                "event_counts": [
                    {
                        "event_type": event_type,
                        "reason": reason,
                        "outcome": outcome,
                        "count": count,
                    }
                    for event_type, reason, outcome, count in event_rows
                ],
                "feedback_counts": dict(feedback_rows),
                "paid_call_totals": [
                    {
                        "purpose": purpose,
                        "call_count": count,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "actual_cost_microusd": cost,
                    }
                    for purpose, count, input_tokens, output_tokens, cost in paid_rows
                ],
                "reply_latency_ms": {
                    "count": len(latencies),
                    "p50": self._percentile(latencies, 0.50),
                    "p95": self._percentile(latencies, 0.95),
                },
                "stopped_reason": trial.stopped_reason,
            }

    @staticmethod
    def _percentile(values: list[int], percentile: float) -> int | None:
        if not values:
            return None
        index = min(len(values) - 1, max(0, int((len(values) - 1) * percentile)))
        return values[index]
