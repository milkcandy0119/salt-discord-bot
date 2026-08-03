"""可使用測試替身驗證的 Discord 訊息處理流程。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.security.sensitive_filter import SensitiveFilter
from app.storage.repositories import MessageRepository, NewMessage

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """與 Discord.py 物件解耦的輸入訊息。"""

    discord_message_id: str
    guild_id: int | None
    channel_id: int
    author_id: int
    author_display_name: str | None
    content: str
    discord_created_at: datetime
    replied_to_message_id: str | None
    author_is_bot: bool
    is_own_message: bool


@dataclass(frozen=True, slots=True)
class SensitiveNotice:
    """不含訊息內容或祕密的通知資料。"""

    discord_message_id: str
    guild_id: int
    channel_id: int
    author_id: int
    categories: tuple[str, ...]


class SensitiveNotifier(Protocol):
    """敏感事件通知介面，刻意不接收原始訊息內容。"""

    async def notify_author(self, notice: SensitiveNotice) -> None: ...

    async def notify_admins(self, notice: SensitiveNotice) -> None: ...


@dataclass(frozen=True, slots=True)
class HandlingOutcome:
    """事件處理結果。"""

    status: str


class MessageHandler:
    """依固定安全順序篩選、遮罩並保存訊息。"""

    def __init__(
        self,
        *,
        repository: MessageRepository,
        sensitive_filter: SensitiveFilter,
        notifier: SensitiveNotifier,
        allowed_guild_ids: frozenset[int],
        allowed_channel_ids: frozenset[int],
    ) -> None:
        self._repository = repository
        self._sensitive_filter = sensitive_filter
        self._notifier = notifier
        self._allowed_guild_ids = allowed_guild_ids
        self._allowed_channel_ids = allowed_channel_ids

    async def handle(self, message: IncomingMessage) -> HandlingOutcome:
        """處理一則事件；外部通知只會收到已移除內容的資料物件。"""

        if (
            message.guild_id is None
            or message.guild_id not in self._allowed_guild_ids
            or message.channel_id not in self._allowed_channel_ids
        ):
            return HandlingOutcome("ignored_not_allowed")
        if message.is_own_message:
            return HandlingOutcome("ignored_own_message")

        content_scan = self._sensitive_filter.scan(message.content)
        display_name_scan = self._sensitive_filter.scan(message.author_display_name or "")
        categories = tuple(
            dict.fromkeys((*content_scan.categories, *display_name_scan.categories))
        )
        is_sensitive = bool(categories)
        save_result = await self._repository.save(
            NewMessage(
                discord_message_id=message.discord_message_id,
                guild_id=str(message.guild_id),
                channel_id=str(message.channel_id),
                author_id=str(message.author_id),
                author_display_name=(
                    display_name_scan.masked_content
                    if message.author_display_name is not None
                    else None
                ),
                content=content_scan.masked_content,
                discord_created_at=message.discord_created_at,
                received_at=datetime.now(UTC),
                replied_to_message_id=message.replied_to_message_id,
                is_bot=message.author_is_bot,
                is_sensitive=is_sensitive,
                sensitive_categories=categories,
            )
        )

        if not save_result.created:
            return HandlingOutcome("duplicate")
        if not is_sensitive:
            return HandlingOutcome("stored")

        notice = SensitiveNotice(
            discord_message_id=message.discord_message_id,
            guild_id=message.guild_id,
            channel_id=message.channel_id,
            author_id=message.author_id,
            categories=categories,
        )
        author_status = await self._send_author_notice(notice)
        admin_status = await self._send_admin_notice(notice)
        await self._repository.update_notification_statuses(
            message.discord_message_id,
            author_status=author_status,
            admin_status=admin_status,
        )
        return HandlingOutcome("stored_sensitive")

    async def _send_author_notice(self, notice: SensitiveNotice) -> str:
        try:
            await self._notifier.notify_author(notice)
        except Exception as error:
            LOGGER.error(
                "敏感事件作者通知失敗 message_id=%s error_type=%s",
                notice.discord_message_id,
                type(error).__name__,
            )
            return "failed"
        return "sent"

    async def _send_admin_notice(self, notice: SensitiveNotice) -> str:
        try:
            await self._notifier.notify_admins(notice)
        except Exception as error:
            LOGGER.error(
                "敏感事件管理員通知失敗 message_id=%s error_type=%s",
                notice.discord_message_id,
                type(error).__name__,
            )
            return "failed"
        return "sent"
