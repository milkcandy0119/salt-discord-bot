"""Discord.py 用戶端與安全通知配接。"""

from __future__ import annotations

import logging

import discord

from app.bot.message_handler import IncomingMessage, MessageHandler, SensitiveNotice
from app.config import Settings
from app.security.sensitive_filter import SensitiveFilter
from app.storage.repositories import MessageRepository

LOGGER = logging.getLogger(__name__)

AUTHOR_NOTICE = (
    "你剛才的訊息可能包含敏感資料。系統未將完整內容寫入資料庫或送往外部服務。"
    "請檢查該訊息，必要時刪除並立即撤銷或更換相關憑證。"
)


class DiscordSensitiveNotifier:
    """只以不含原文的固定訊息通知作者與管理員。"""

    def __init__(self, client: discord.Client, notification_user_ids: frozenset[int]) -> None:
        self._client = client
        self._notification_user_ids = notification_user_ids

    async def _get_user(self, user_id: int) -> discord.User:
        cached_user = self._client.get_user(user_id)
        return cached_user or await self._client.fetch_user(user_id)

    async def notify_author(self, notice: SensitiveNotice) -> None:
        """私訊作者固定安全提醒。"""

        user = await self._get_user(notice.author_id)
        await user.send(AUTHOR_NOTICE)

    async def notify_admins(self, notice: SensitiveNotice) -> None:
        """通知擁有者與設定的管理員，不附帶訊息內容。"""

        category_text = ", ".join(notice.categories)
        admin_notice = (
            "已攔截一則可能含敏感資料的訊息。"
            f"guild_id={notice.guild_id} channel_id={notice.channel_id} "
            f"message_id={notice.discord_message_id} author_id={notice.author_id} "
            f"categories={category_text}"
        )
        failures = 0
        for user_id in self._notification_user_ids:
            try:
                user = await self._get_user(user_id)
                await user.send(admin_notice)
            except discord.DiscordException:
                failures += 1
        if failures:
            raise RuntimeError(f"有 {failures} 位管理員通知失敗")


class DiscordAssistantClient(discord.Client):
    """將 Discord 事件轉為內部訊息後交給安全處理流程。"""

    def __init__(self, *, settings: Settings, repository: MessageRepository) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        notifier = DiscordSensitiveNotifier(self, settings.sensitive_notification_user_ids)
        self._message_handler = MessageHandler(
            repository=repository,
            sensitive_filter=SensitiveFilter(),
            notifier=notifier,
            allowed_guild_ids=settings.allowed_guild_ids,
            allowed_channel_ids=settings.allowed_channel_ids,
        )

    async def on_ready(self) -> None:
        """記錄連線完成；日誌只包含必要 ID。"""

        if self.user is not None:
            LOGGER.info("Discord 連線完成 bot_user_id=%s", self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        """接收 Discord 訊息，忽略私訊、非白名單與機器人自己的訊息。"""

        if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
            return

        replied_to_message_id = None
        if message.reference is not None and message.reference.message_id is not None:
            replied_to_message_id = str(message.reference.message_id)

        incoming = IncomingMessage(
            discord_message_id=str(message.id),
            guild_id=message.guild.id if message.guild is not None else None,
            channel_id=message.channel.id,
            author_id=message.author.id,
            author_display_name=getattr(message.author, "display_name", None),
            content=message.content,
            discord_created_at=message.created_at,
            replied_to_message_id=replied_to_message_id,
            author_is_bot=message.author.bot,
            is_own_message=self.user is not None and message.author.id == self.user.id,
        )
        try:
            await self._message_handler.handle(incoming)
        except Exception as error:
            LOGGER.error(
                "Discord 訊息處理失敗 message_id=%s error_type=%s",
                message.id,
                type(error).__name__,
            )
