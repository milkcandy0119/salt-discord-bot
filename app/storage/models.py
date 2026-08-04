"""SQLAlchemy 資料模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
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


class PersonalMemoryRecord(Base):
    """由使用者自己建立、按 Discord user ID 隔離的基本記憶。"""

    __tablename__ = "personal_memories"
    __table_args__ = (
        UniqueConstraint(
            "guild_id",
            "user_id",
            "normalized_content",
            name="uq_personal_memory_owner_content",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_content: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserTimezoneRecord(Base):
    """使用者在單一 Discord 伺服器中的提醒時區。"""

    __tablename__ = "user_timezones"
    __table_args__ = (
        UniqueConstraint("guild_id", "user_id", name="uq_user_timezone_owner"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReminderRecord(Base):
    """可在重啟後恢復並以私訊派送的使用者提醒。"""

    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AdminAuditEventRecord(Base):
    """不保存被查看內容的管理操作稽核事件。"""

    __tablename__ = "admin_audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    actor_user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_user_id: Mapped[str | None] = mapped_column(String(32), index=True)
    target_record_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TrialSessionRecord(Base):
    """階段 9 試跑範圍、基準、增量上限及生命週期。"""

    __tablename__ = "trial_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    guild_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    channel_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    companion_channel_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    timezone_name: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_global_committed_microusd: Mapped[int] = mapped_column(Integer, nullable=False)
    baseline_background_committed_microusd: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    global_increment_limit_microusd: Mapped[int] = mapped_column(Integer, nullable=False)
    background_increment_limit_microusd: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    companion_daily_reply_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stopped_reason: Mapped[str | None] = mapped_column(String(64))
    final_global_increment_microusd: Mapped[int | None] = mapped_column(Integer)
    final_background_increment_microusd: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TrialEventRecord(Base):
    """不含聊天內容、作者名稱或模型輸出的試跑觀測事件。"""

    __tablename__ = "trial_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("trial_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    guild_id: Mapped[str | None] = mapped_column(String(32), index=True)
    channel_id: Mapped[str | None] = mapped_column(String(32), index=True)
    message_id: Mapped[str | None] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel_mode: Mapped[str | None] = mapped_column(String(16), index=True)
    trigger_kind: Mapped[str | None] = mapped_column(String(32), index=True)
    reason: Mapped[str | None] = mapped_column(String(64), index=True)
    outcome: Mapped[str | None] = mapped_column(String(64), index=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class TrialDailyCounterRecord(Base):
    """以試跑時區日期原子限制 companion 自動回覆數。"""

    __tablename__ = "trial_daily_counters"
    __table_args__ = (
        UniqueConstraint("session_id", "local_date", name="uq_trial_daily_counter"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("trial_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    local_date: Mapped[str] = mapped_column(String(10), nullable=False)
    companion_reply_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TrialFeedbackRecord(Base):
    """管理員以固定分類標記訊息，不重複保存訊息內容。"""

    __tablename__ = "trial_feedback"
    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "actor_user_id",
            "target_message_id",
            "category",
            name="uq_trial_feedback_once",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("trial_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    guild_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    actor_user_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    target_message_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


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


class SegmentSummaryRecord(Base):
    """可由原始訊息重建的封存段落摘要。"""

    __tablename__ = "segment_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    segment_id: Mapped[int] = mapped_column(
        ForeignKey("conversation_segments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_through_message_record_id: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_response_id: Mapped[str] = mapped_column(String(128), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SummaryEmbeddingRecord(Base):
    """段落摘要分塊的 SQLite BLOB 向量索引。"""

    __tablename__ = "summary_embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    summary_id: Mapped[int] = mapped_column(
        ForeignKey("segment_summaries.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_text_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    vector_norm: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BackgroundJobRecord(Base):
    """可在重啟後繼續執行的摘要與向量化工作。"""

    __tablename__ = "background_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    segment_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversation_segments.id", ondelete="CASCADE"),
        index=True,
    )
    summary_id: Mapped[int | None] = mapped_column(
        ForeignKey("segment_summaries.id", ondelete="CASCADE"),
        index=True,
    )
    source_through_message_record_id: Mapped[int | None] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
