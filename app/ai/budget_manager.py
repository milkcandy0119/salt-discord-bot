"""交易安全的一次性預算帳本與付費呼叫閘門。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import (
    BudgetStateRecord,
    BudgetThresholdNotificationRecord,
    PaidAiCallRecord,
    TrialSessionRecord,
)

MICROUSD_PER_USD = 1_000_000
GLOBAL_LIMIT_MICROUSD = 10 * MICROUSD_PER_USD
BACKGROUND_LIMIT_MICROUSD = 3 * MICROUSD_PER_USD
THRESHOLD_AMOUNTS = {70: 7 * MICROUSD_PER_USD, 90: 9 * MICROUSD_PER_USD}
_ERROR_CODE_PATTERN = re.compile(r"[a-z0-9_]{1,64}")


class PaidPurpose(StrEnum):
    """每筆付費呼叫必須使用的用途分類。"""

    FOREGROUND_CHAT = "foreground_chat"
    SUMMARY = "summary"
    EMBEDDING = "embedding"
    SEMANTIC_SEGMENTATION = "semantic_segmentation"

    @property
    def is_background(self) -> bool:
        """指出此用途是否受背景 US$3 上限約束。"""

        return self in {self.SUMMARY, self.EMBEDDING}


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """特定模型及價格版本的整數微美元快照。"""

    model_name: str
    price_version: str
    input_microusd_per_million_tokens: int
    output_microusd_per_million_tokens: int

    def __post_init__(self) -> None:
        if not self.model_name.strip() or not self.price_version.strip():
            raise ValueError("模型名稱與價格版本不得為空")
        if (
            self.input_microusd_per_million_tokens < 0
            or self.output_microusd_per_million_tokens < 0
        ):
            raise ValueError("Token 價格不得為負數")

    def quote(self, *, input_tokens: int, output_tokens: int) -> int:
        """以向上取整方式計算精確整數微美元費用。"""

        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("Token 數不得為負數")
        numerator = (
            input_tokens * self.input_microusd_per_million_tokens
            + output_tokens * self.output_microusd_per_million_tokens
        )
        if numerator == 0:
            return 0
        return (numerator + 999_999) // 1_000_000


@dataclass(frozen=True, slots=True)
class Reservation:
    """付費呼叫開始前成功取得的額度預留。"""

    reservation_id: str
    purpose: PaidPurpose
    reserved_cost_microusd: int


@dataclass(frozen=True, slots=True)
class Settlement:
    """依實際 Token 用量完成的結算結果。"""

    reservation_id: str
    actual_cost_microusd: int
    status: str


@dataclass(frozen=True, slots=True)
class BudgetSnapshot:
    """不包含任何祕密的預算彙總快照。"""

    global_spent_microusd: int
    global_reserved_microusd: int
    background_spent_microusd: int
    background_reserved_microusd: int

    @property
    def global_committed_microusd(self) -> int:
        """傳回全域已花費加尚未完成預留的總額。"""

        return self.global_spent_microusd + self.global_reserved_microusd


@dataclass(frozen=True, slots=True)
class PaidCallView:
    """供管理與測試讀取的付費呼叫紀錄。"""

    reservation_id: str
    purpose: str
    model_name: str
    price_version: str
    reserved_cost_microusd: int
    actual_cost_microusd: int | None
    input_tokens: int | None
    output_tokens: int | None
    status: str


class BudgetThresholdNotifier(Protocol):
    """70%／90% 通知介面。"""

    async def notify_threshold(
        self,
        threshold_percent: int,
        snapshot: BudgetSnapshot,
    ) -> None: ...


class BudgetExceededError(RuntimeError):
    """預留會突破全域或背景上限。"""

    def __init__(self, limit_name: str) -> None:
        self.limit_name = limit_name
        super().__init__(f"預算不足：{limit_name}")


class ReservationStateError(RuntimeError):
    """預留紀錄不允許目前要求的狀態轉換。"""


class BudgetManager:
    """所有未來付費 AI 服務唯一允許使用的預算入口。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def reserve(
        self,
        *,
        purpose: PaidPurpose,
        price: ModelPrice,
        maximum_input_tokens: int,
        maximum_output_tokens: int,
    ) -> Reservation:
        """以資料庫條件更新原子地預留最大估算費用。"""

        estimated_cost = price.quote(
            input_tokens=maximum_input_tokens,
            output_tokens=maximum_output_tokens,
        )
        if estimated_cost <= 0:
            raise ValueError("付費呼叫的預估費用必須大於零")

        reservation_id = str(uuid4())
        now = datetime.now(UTC)
        background = purpose.is_background
        async with self._session_factory() as session, session.begin():
            trial = await session.scalar(
                select(TrialSessionRecord)
                .order_by(TrialSessionRecord.id.desc())
                .limit(1)
            )
            if trial is not None:
                trial_ends_at = (
                    trial.ends_at.replace(tzinfo=UTC)
                    if trial.ends_at.tzinfo is None
                    else trial.ends_at.astimezone(UTC)
                )
                if trial.status != "active":
                    raise BudgetExceededError("trial_inactive")
                if now >= trial_ends_at:
                    raise BudgetExceededError("trial_ended")
            conditions = [
                BudgetStateRecord.id == 1,
                BudgetStateRecord.global_spent_microusd
                + BudgetStateRecord.global_reserved_microusd
                + estimated_cost
                <= GLOBAL_LIMIT_MICROUSD,
            ]
            if trial is not None:
                conditions.append(
                    BudgetStateRecord.global_spent_microusd
                    + BudgetStateRecord.global_reserved_microusd
                    + estimated_cost
                    <= trial.baseline_global_committed_microusd
                    + trial.global_increment_limit_microusd
                )
            values: dict[str, object] = {
                "global_reserved_microusd": (
                    BudgetStateRecord.global_reserved_microusd + estimated_cost
                ),
                "updated_at": now,
            }
            if background:
                conditions.append(
                    BudgetStateRecord.background_spent_microusd
                    + BudgetStateRecord.background_reserved_microusd
                    + estimated_cost
                    <= BACKGROUND_LIMIT_MICROUSD
                )
                values["background_reserved_microusd"] = (
                    BudgetStateRecord.background_reserved_microusd + estimated_cost
                )
                if trial is not None:
                    conditions.append(
                        BudgetStateRecord.background_spent_microusd
                        + BudgetStateRecord.background_reserved_microusd
                        + estimated_cost
                        <= trial.baseline_background_committed_microusd
                        + trial.background_increment_limit_microusd
                    )

            result = await session.execute(
                update(BudgetStateRecord).where(*conditions).values(**values)
            )
            if result.rowcount != 1:
                state = await session.get(BudgetStateRecord, 1)
                if state is None:
                    raise RuntimeError("找不到預算狀態，請先執行 migration")
                if (
                    background
                    and state.background_spent_microusd
                    + state.background_reserved_microusd
                    + estimated_cost
                    > BACKGROUND_LIMIT_MICROUSD
                ):
                    raise BudgetExceededError("background")
                if (
                    trial is not None
                    and background
                    and state.background_spent_microusd
                    + state.background_reserved_microusd
                    + estimated_cost
                    > trial.baseline_background_committed_microusd
                    + trial.background_increment_limit_microusd
                ):
                    raise BudgetExceededError("trial_background")
                if (
                    trial is not None
                    and state.global_spent_microusd
                    + state.global_reserved_microusd
                    + estimated_cost
                    > trial.baseline_global_committed_microusd
                    + trial.global_increment_limit_microusd
                ):
                    raise BudgetExceededError("trial_global")
                raise BudgetExceededError("global")

            session.add(
                PaidAiCallRecord(
                    reservation_id=reservation_id,
                    purpose=purpose.value,
                    budget_scope="background" if background else "foreground",
                    model_name=price.model_name,
                    price_version=price.price_version,
                    input_microusd_per_million_tokens=(
                        price.input_microusd_per_million_tokens
                    ),
                    output_microusd_per_million_tokens=(
                        price.output_microusd_per_million_tokens
                    ),
                    maximum_input_tokens=maximum_input_tokens,
                    maximum_output_tokens=maximum_output_tokens,
                    reserved_cost_microusd=estimated_cost,
                    status="reserved",
                    created_at=now,
                )
            )

        return Reservation(reservation_id, purpose, estimated_cost)

    async def settle(
        self,
        reservation_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> Settlement:
        """依預留時的價格快照結算實際 Token 用量。"""

        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            claimed = (
                await session.execute(
                    update(PaidAiCallRecord)
                    .where(
                        PaidAiCallRecord.reservation_id == reservation_id,
                        PaidAiCallRecord.status.in_(("reserved", "usage_uncertain")),
                    )
                    .values(status="settling")
                    .returning(PaidAiCallRecord)
                )
            ).scalar_one_or_none()
            if claimed is None:
                existing = await session.get(PaidAiCallRecord, reservation_id)
                return self._existing_settlement(existing, input_tokens, output_tokens)

            price = ModelPrice(
                model_name=claimed.model_name,
                price_version=claimed.price_version,
                input_microusd_per_million_tokens=(
                    claimed.input_microusd_per_million_tokens
                ),
                output_microusd_per_million_tokens=(
                    claimed.output_microusd_per_million_tokens
                ),
            )
            actual_cost = price.quote(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            status = (
                "settled_over_reservation"
                if actual_cost > claimed.reserved_cost_microusd
                else "settled"
            )
            state_values: dict[str, object] = {
                "global_reserved_microusd": (
                    BudgetStateRecord.global_reserved_microusd
                    - claimed.reserved_cost_microusd
                ),
                "global_spent_microusd": (
                    BudgetStateRecord.global_spent_microusd + actual_cost
                ),
                "updated_at": now,
            }
            if claimed.budget_scope == "background":
                state_values.update(
                    background_reserved_microusd=(
                        BudgetStateRecord.background_reserved_microusd
                        - claimed.reserved_cost_microusd
                    ),
                    background_spent_microusd=(
                        BudgetStateRecord.background_spent_microusd + actual_cost
                    ),
                )
            new_global_spent = (
                await session.execute(
                    update(BudgetStateRecord)
                    .where(BudgetStateRecord.id == 1)
                    .values(**state_values)
                    .returning(BudgetStateRecord.global_spent_microusd)
                )
            ).scalar_one()
            await session.execute(
                update(PaidAiCallRecord)
                .where(PaidAiCallRecord.reservation_id == reservation_id)
                .values(
                    actual_cost_microusd=actual_cost,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    status=status,
                    error_code=None,
                    finalized_at=now,
                )
            )
            await self._create_due_notifications(session, new_global_spent, now)

        return Settlement(reservation_id, actual_cost, status)

    def _existing_settlement(
        self,
        existing: PaidAiCallRecord | None,
        input_tokens: int,
        output_tokens: int,
    ) -> Settlement:
        if existing is None:
            raise LookupError("找不到預留紀錄")
        if existing.status in {"settled", "settled_over_reservation"}:
            if existing.input_tokens == input_tokens and existing.output_tokens == output_tokens:
                return Settlement(
                    existing.reservation_id,
                    existing.actual_cost_microusd or 0,
                    existing.status,
                )
            raise ReservationStateError("已結算紀錄不得以不同用量重新結算")
        raise ReservationStateError(f"目前狀態不可結算：{existing.status}")

    async def release_unbilled(self, reservation_id: str, *, error_code: str) -> bool:
        """僅在確定請求未產生費用時釋放預留。"""

        self._validate_error_code(error_code)
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            claimed = (
                await session.execute(
                    update(PaidAiCallRecord)
                    .where(
                        PaidAiCallRecord.reservation_id == reservation_id,
                        PaidAiCallRecord.status.in_(("reserved", "usage_uncertain")),
                    )
                    .values(status="releasing")
                    .returning(PaidAiCallRecord)
                )
            ).scalar_one_or_none()
            if claimed is None:
                existing = await session.get(PaidAiCallRecord, reservation_id)
                if existing is not None and existing.status == "released_unbilled":
                    return False
                raise ReservationStateError("目前狀態不可釋放")

            state_values: dict[str, object] = {
                "global_reserved_microusd": (
                    BudgetStateRecord.global_reserved_microusd
                    - claimed.reserved_cost_microusd
                ),
                "updated_at": now,
            }
            if claimed.budget_scope == "background":
                state_values["background_reserved_microusd"] = (
                    BudgetStateRecord.background_reserved_microusd
                    - claimed.reserved_cost_microusd
                )
            await session.execute(
                update(BudgetStateRecord)
                .where(BudgetStateRecord.id == 1)
                .values(**state_values)
            )
            await session.execute(
                update(PaidAiCallRecord)
                .where(PaidAiCallRecord.reservation_id == reservation_id)
                .values(
                    actual_cost_microusd=0,
                    status="released_unbilled",
                    error_code=error_code,
                    finalized_at=now,
                )
            )
        return True

    async def mark_usage_uncertain(self, reservation_id: str, *, error_code: str) -> bool:
        """用量不明時保留完整預留額度，等待後續核對。"""

        self._validate_error_code(error_code)
        async with self._session_factory() as session, session.begin():
            result = await session.execute(
                update(PaidAiCallRecord)
                .where(
                    PaidAiCallRecord.reservation_id == reservation_id,
                    PaidAiCallRecord.status == "reserved",
                )
                .values(status="usage_uncertain", error_code=error_code)
            )
            if result.rowcount == 1:
                return True
            existing = await session.get(PaidAiCallRecord, reservation_id)
            if existing is not None and existing.status == "usage_uncertain":
                return False
            raise ReservationStateError("目前狀態不可標記為用量不明")

    async def get_snapshot(self) -> BudgetSnapshot:
        """讀取目前實際花費與尚未完成的預留。"""

        async with self._session_factory() as session:
            state = await session.get(BudgetStateRecord, 1)
            if state is None:
                raise RuntimeError("找不到預算狀態，請先執行 migration")
            return BudgetSnapshot(
                state.global_spent_microusd,
                state.global_reserved_microusd,
                state.background_spent_microusd,
                state.background_reserved_microusd,
            )

    async def get_call(self, reservation_id: str) -> PaidCallView | None:
        """讀取單筆不含祕密的付費呼叫紀錄。"""

        async with self._session_factory() as session:
            record = await session.get(PaidAiCallRecord, reservation_id)
            if record is None:
                return None
            return PaidCallView(
                reservation_id=record.reservation_id,
                purpose=record.purpose,
                model_name=record.model_name,
                price_version=record.price_version,
                reserved_cost_microusd=record.reserved_cost_microusd,
                actual_cost_microusd=record.actual_cost_microusd,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                status=record.status,
            )

    async def _create_due_notifications(
        self,
        session: AsyncSession,
        global_spent_microusd: int,
        now: datetime,
    ) -> None:
        for threshold_percent, amount in THRESHOLD_AMOUNTS.items():
            if global_spent_microusd < amount:
                continue
            await session.execute(
                sqlite_insert(BudgetThresholdNotificationRecord)
                .values(
                    threshold_percent=threshold_percent,
                    status="pending",
                    attempts=0,
                    created_at=now,
                )
                .on_conflict_do_nothing(index_elements=["threshold_percent"])
            )

    async def dispatch_pending_notifications(
        self,
        notifier: BudgetThresholdNotifier,
    ) -> tuple[int, ...]:
        """逐一領取並傳送尚未完成的預算門檻通知。"""

        sent: list[int] = []
        while True:
            threshold = await self._claim_pending_notification()
            if threshold is None:
                return tuple(sent)
            try:
                await notifier.notify_threshold(threshold, await self.get_snapshot())
            except Exception as error:
                await self._return_notification_to_pending(threshold, type(error).__name__)
                raise
            await self._mark_notification_sent(threshold)
            sent.append(threshold)

    async def _claim_pending_notification(self) -> int | None:
        now = datetime.now(UTC)
        candidate = (
            select(BudgetThresholdNotificationRecord.threshold_percent)
            .where(BudgetThresholdNotificationRecord.status == "pending")
            .order_by(BudgetThresholdNotificationRecord.threshold_percent)
            .limit(1)
            .scalar_subquery()
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(BudgetThresholdNotificationRecord)
                .where(
                    BudgetThresholdNotificationRecord.status == "sending",
                    BudgetThresholdNotificationRecord.claimed_at
                    < now - timedelta(minutes=5),
                )
                .values(status="pending", claimed_at=None)
            )
            return (
                await session.execute(
                    update(BudgetThresholdNotificationRecord)
                    .where(
                        BudgetThresholdNotificationRecord.threshold_percent == candidate,
                        BudgetThresholdNotificationRecord.status == "pending",
                    )
                    .values(
                        status="sending",
                        attempts=BudgetThresholdNotificationRecord.attempts + 1,
                        claimed_at=now,
                        last_error_type=None,
                    )
                    .returning(BudgetThresholdNotificationRecord.threshold_percent)
                )
            ).scalar_one_or_none()

    async def _mark_notification_sent(self, threshold_percent: int) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(BudgetThresholdNotificationRecord)
                .where(
                    BudgetThresholdNotificationRecord.threshold_percent == threshold_percent,
                    BudgetThresholdNotificationRecord.status == "sending",
                )
                .values(status="sent", sent_at=datetime.now(UTC))
            )

    async def _return_notification_to_pending(
        self,
        threshold_percent: int,
        error_type: str,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(
                update(BudgetThresholdNotificationRecord)
                .where(
                    BudgetThresholdNotificationRecord.threshold_percent == threshold_percent,
                    BudgetThresholdNotificationRecord.status == "sending",
                )
                .values(
                    status="pending",
                    claimed_at=None,
                    last_error_type=error_type[:128],
                )
            )

    async def get_notification_statuses(self) -> dict[int, str]:
        """讀取已建立的門檻通知狀態。"""

        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        BudgetThresholdNotificationRecord.threshold_percent,
                        BudgetThresholdNotificationRecord.status,
                    ).order_by(BudgetThresholdNotificationRecord.threshold_percent)
                )
            ).all()
            return dict(rows)

    @staticmethod
    def _validate_error_code(error_code: str) -> None:
        if _ERROR_CODE_PATTERN.fullmatch(error_code) is None:
            raise ValueError("錯誤代碼只能包含小寫英數字與底線，長度最多 64")
