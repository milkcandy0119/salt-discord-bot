"""Discord.py 用戶端與安全通知配接。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import discord
from discord.ext import tasks

from app.ai.budget_manager import BudgetManager, BudgetSnapshot
from app.ai.chat_service import ChatService
from app.bot.channel_modes import (
    ChannelMode,
    ChannelModeResolver,
    ReplySignals,
    ReplyTriggerPolicy,
)
from app.bot.companion_scheduler import CompanionScheduler
from app.bot.message_handler import IncomingMessage, MessageHandler, SensitiveNotice
from app.config import Settings
from app.conversations.context_builder import ContextBuilder
from app.conversations.segmenter import ConversationSegmenter
from app.security.sensitive_filter import SensitiveFilter
from app.storage.repositories import MessageRepository, NewMessage

LOGGER = logging.getLogger(__name__)

AUTHOR_NOTICE = (
    "你剛才的訊息可能包含敏感資料。系統未將完整內容寫入資料庫或送往外部服務。"
    "請檢查該訊息，必要時刪除並立即撤銷或更換相關憑證。"
)


class DiscordBudgetThresholdNotifier:
    """將預算門檻通知私訊給唯一設定的機器人擁有者。"""

    def __init__(self, client: discord.Client, owner_user_id: int) -> None:
        self._client = client
        self._owner_user_id = owner_user_id

    async def notify_threshold(
        self,
        threshold_percent: int,
        snapshot: BudgetSnapshot,
    ) -> None:
        """傳送不含模型內容或祕密的管理通知。"""

        del snapshot
        user = self._client.get_user(self._owner_user_id)
        if user is None:
            user = await self._client.fetch_user(self._owner_user_id)
        await user.send(
            f"Discord 助手的一次性 AI 預算實際用量已達 {threshold_percent}%。"
            "這是管理資訊，請檢查預算狀態。"
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

    def __init__(
        self,
        *,
        settings: Settings,
        repository: MessageRepository,
        segmenter: ConversationSegmenter,
        budget_manager: BudgetManager,
        context_builder: ContextBuilder,
        chat_service: ChatService,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)

        notifier = DiscordSensitiveNotifier(self, settings.sensitive_notification_user_ids)
        self._settings = settings
        self._repository = repository
        self._segmenter = segmenter
        self._budget_manager = budget_manager
        self._context_builder = context_builder
        self._chat_service = chat_service
        self._budget_notifier = DiscordBudgetThresholdNotifier(self, settings.owner_user_id)
        self._mode_resolver = ChannelModeResolver(
            allowed_channel_ids=settings.allowed_channel_ids,
            companion_channel_ids=settings.companion_channel_ids,
        )
        self._trigger_policy = ReplyTriggerPolicy(
            companion_cooldown=timedelta(seconds=settings.companion_cooldown_seconds)
        )
        self._companion_scheduler = CompanionScheduler(
            observation_window=timedelta(seconds=settings.companion_observation_seconds)
        )
        self._last_companion_reply_at: dict[int, datetime] = {}
        self._message_handler = MessageHandler(
            repository=repository,
            sensitive_filter=SensitiveFilter(),
            notifier=notifier,
            segmenter=segmenter,
            allowed_guild_ids=settings.allowed_guild_ids,
            allowed_channel_ids=settings.allowed_channel_ids,
        )

    async def setup_hook(self) -> None:
        """補處理重啟前未切段訊息，並啟動定期封存檢查。"""

        recovered = await self._segmenter.assign_pending_messages()
        if recovered:
            LOGGER.info("已補處理未切段訊息 count=%s", recovered)
        self.archive_inactive_segments.start()
        self.dispatch_budget_notifications.start()

    async def close(self) -> None:
        """停止定期封存工作並關閉 Discord 連線。"""

        await self._companion_scheduler.close()
        if self.archive_inactive_segments.is_running():
            self.archive_inactive_segments.cancel()
        if self.dispatch_budget_notifications.is_running():
            self.dispatch_budget_notifications.cancel()
        await super().close()

    @tasks.loop(minutes=1)
    async def archive_inactive_segments(self) -> None:
        """每分鐘封存滿 30 分鐘沒有新訊息的段落。"""

        try:
            await self._segmenter.archive_inactive()
        except Exception as error:
            LOGGER.error("段落封存檢查失敗 error_type=%s", type(error).__name__)

    @tasks.loop(minutes=1)
    async def dispatch_budget_notifications(self) -> None:
        """每分鐘傳送尚未完成的 70%／90% 預算通知。"""

        try:
            await self._budget_manager.dispatch_pending_notifications(self._budget_notifier)
        except Exception as error:
            LOGGER.error("預算門檻通知失敗 error_type=%s", type(error).__name__)

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
            sticker_names=tuple(
                sticker.name
                for sticker in getattr(message, "stickers", ())
                if isinstance(getattr(sticker, "name", None), str)
            ),
        )
        try:
            outcome = await self._message_handler.handle(incoming)
        except Exception as error:
            LOGGER.error(
                "Discord 訊息處理失敗 message_id=%s error_type=%s",
                message.id,
                type(error).__name__,
            )
            return

        if outcome.status != "stored" or incoming.author_is_bot:
            return
        try:
            await self._route_ai_reply(message, incoming)
        except Exception as error:
            LOGGER.error(
                "AI 回覆流程失敗 message_id=%s error_type=%s",
                message.id,
                type(error).__name__,
            )

    async def _route_ai_reply(
        self,
        message: discord.Message,
        incoming: IncomingMessage,
    ) -> None:
        """依 channel ID 模式選擇立即回覆或陪伴觀察。"""

        mode = self._mode_resolver.resolve(incoming.channel_id)
        if mode is None or self.user is None:
            return
        explicit_signals = ReplySignals(
            channel_id=incoming.channel_id,
            content=incoming.content,
            mentioned_bot=any(user.id == self.user.id for user in message.mentions),
            replied_to_bot=await self._is_reply_to_this_bot(message, incoming),
            is_command=self._is_ai_command(incoming.content),
            now=datetime.now(UTC),
        )
        explicit_decision = self._trigger_policy.decide(
            ChannelMode.NORMAL,
            explicit_signals,
        )
        if explicit_decision.should_reply:
            self._companion_scheduler.cancel(incoming.channel_id)
            await self._send_ai_reply(message, incoming, companion_generated=False)
            return
        if mode is ChannelMode.COMPANION:
            self._companion_scheduler.schedule(
                incoming.channel_id,
                lambda: self._evaluate_companion_reply_safely(message, incoming),
            )

    async def _evaluate_companion_reply_safely(
        self,
        message: discord.Message,
        incoming: IncomingMessage,
    ) -> None:
        """攔截背景陪伴工作錯誤，避免未處理的非同步例外。"""

        try:
            await self._evaluate_companion_reply(message, incoming)
        except Exception as error:
            LOGGER.error(
                "陪伴模式回覆評估失敗 message_id=%s error_type=%s",
                incoming.discord_message_id,
                type(error).__name__,
            )

    async def _evaluate_companion_reply(
        self,
        message: discord.Message,
        incoming: IncomingMessage,
    ) -> None:
        """頻道安靜滿觀察窗後，以免費訊號決定是否加入對話。"""

        if self.user is None:
            return
        now = datetime.now(UTC)
        longest_window = timedelta(minutes=self._settings.companion_recent_bot_minutes)
        recent = await self._repository.list_recent_in_channel(
            str(incoming.channel_id),
            since=now - longest_window,
        )
        human_cutoff = now - timedelta(
            seconds=self._settings.companion_activity_window_seconds
        )
        bot_cutoff = now - longest_window
        human_ids = frozenset(
            int(record.author_id)
            for record in recent
            if not record.is_bot and self._as_utc(record.discord_created_at) >= human_cutoff
        )
        bot_spoke_recently = any(
            record.author_id == str(self.user.id)
            and self._as_utc(record.discord_created_at) >= bot_cutoff
            for record in recent
        )
        decision = self._trigger_policy.decide(
            ChannelMode.COMPANION,
            ReplySignals(
                channel_id=incoming.channel_id,
                content=incoming.content,
                recent_human_author_ids=human_ids,
                bot_spoke_recently=bot_spoke_recently,
                last_companion_reply_at=self._last_companion_reply_at.get(
                    incoming.channel_id
                ),
                now=now,
            ),
        )
        if decision.should_reply:
            await self._send_ai_reply(message, incoming, companion_generated=True)

    async def _send_ai_reply(
        self,
        message: discord.Message,
        incoming: IncomingMessage,
        *,
        companion_generated: bool,
    ) -> None:
        """建立上下文、產生回覆、傳送 Discord 並保存回覆關係。"""

        if self.user is None:
            return
        context = await self._context_builder.build(
            incoming.discord_message_id,
            assistant_author_id=str(self.user.id),
        )
        outcome = await self._chat_service.generate(context)
        outgoing_content = self._fit_discord_message(outcome.content)
        try:
            sent = await message.reply(
                outgoing_content,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            LOGGER.error(
                "Discord AI 回覆傳送失敗 trigger_message_id=%s status=%s error_type=%s",
                incoming.discord_message_id,
                outcome.status,
                type(error).__name__,
            )
            return

        try:
            save_result = await self._repository.save(
                NewMessage(
                    discord_message_id=str(sent.id),
                    guild_id=str(incoming.guild_id),
                    channel_id=str(incoming.channel_id),
                    author_id=str(self.user.id),
                    author_display_name=getattr(self.user, "display_name", self.user.name),
                    content=outgoing_content,
                    discord_created_at=sent.created_at,
                    received_at=datetime.now(UTC),
                    replied_to_message_id=incoming.discord_message_id,
                    is_bot=True,
                    is_sensitive=False,
                    sensitive_categories=(),
                )
            )
            if save_result.created:
                await self._segmenter.assign_message(str(sent.id))
        except Exception as error:
            LOGGER.error(
                "Discord AI 回覆保存失敗 reply_message_id=%s error_type=%s",
                sent.id,
                type(error).__name__,
            )
            return

        if companion_generated:
            self._last_companion_reply_at[incoming.channel_id] = datetime.now(UTC)
        LOGGER.info(
            "Discord AI 回覆完成 trigger_message_id=%s reply_message_id=%s status=%s",
            incoming.discord_message_id,
            sent.id,
            outcome.status,
        )

    async def _is_reply_to_this_bot(
        self,
        message: discord.Message,
        incoming: IncomingMessage,
    ) -> bool:
        if self.user is None or incoming.replied_to_message_id is None:
            return False
        stored = await self._repository.get_by_discord_id(incoming.replied_to_message_id)
        if stored is not None:
            return stored.author_id == str(self.user.id)
        reference = message.reference
        resolved = reference.resolved if reference is not None else None
        return isinstance(resolved, discord.Message) and resolved.author.id == self.user.id

    def _is_ai_command(self, content: str) -> bool:
        words = content.strip().split(maxsplit=1)
        configured_prefix = self._settings.discord_ai_command_prefix.casefold()
        return bool(words) and words[0].casefold() == configured_prefix

    @staticmethod
    def _fit_discord_message(content: str) -> str:
        """保留 Discord 單則訊息上限的安全餘裕。"""

        if len(content) <= 1_900:
            return content
        return f"{content[:1899]}…"

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
