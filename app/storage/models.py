"""SQLAlchemy 資料模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有資料模型共用的宣告基底。"""


class MessageRecord(Base):
    """已接收的 Discord 訊息及階段 1 處理狀態。"""

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
    processing_status: Mapped[str] = mapped_column(String(32), nullable=False, default="stored")
    author_notification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_required"
    )
    admin_notification_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_required"
    )

