"""SQLAlchemy 資料模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有資料模型共用的宣告基底。"""


class ConversationSegmentRecord(Base):
    """同一頻道中可獨立活動或封存的對話段落。"""

    __tablename__ = "conversation_segments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    root_message_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_message_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MessageRecord(Base):
    """已接收的 Discord 訊息、安全狀態及對話段落關係。"""

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    discord_message_id: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    author_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    author_display_name: Mapped[str | None] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    discord_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    replied_to_message_id: Mapped[str | None] = mapped_column(String(32), index=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    sensitive_categories: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    processing_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending_segmentation"
    )
    author_notification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_required"
    )
    admin_notification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_required"
    )
    segment_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation_segments.id", ondelete="SET NULL"),
        index=True,
    )


class BudgetStateRecord(Base):
    """整個系統唯一的一次性預算彙總狀態。"""

    __tablename__ = "budget_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    global_spent_microusd: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    global_reserved_microusd: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    background_spent_microusd: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    background_reserved_microusd: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PaidAiCallRecord(Base):
    """付費 AI 呼叫的預留、價格快照、用量及結算紀錄。"""

    __tablename__ = "paid_ai_calls"

    reservation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    budget_scope: Mapped[str] = mapped_column(String(16), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    price_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_microusd_per_million_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_microusd_per_million_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_cost_microusd: Mapped[int] = mapped_column(Integer, nullable=False)
    actual_cost_microusd: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BudgetThresholdNotificationRecord(Base):
    """70% 與 90% 預算門檻的一次性通知狀態。"""

    __tablename__ = "budget_threshold_notifications"

    threshold_percent: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_type: Mapped[str | None] = mapped_column(String(128))
