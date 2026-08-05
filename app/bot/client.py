"""Discord.py 用戶端與安全通知配接。"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from time import perf_counter

import discord
from discord.ext import tasks

from app.ai.budget_manager import BudgetManager, BudgetSnapshot
from app.ai.chat_service import ChatService
from app.ai.persona import Persona
from app.bot.admin_commands import BotAdminCommandGroup
from app.bot.admin_memory_commands import AdminMemoryCommandGroup
from app.bot.channel_modes import (
    ChannelMode,
    ChannelModeResolver,
    ReplySignals,
    ReplyTriggerPolicy,
)
from app.bot.companion_scheduler import CompanionScheduler
from app.bot.global_commands import SaltGlobalCommandGroup
from app.bot.memory_commands import PersonalMemoryCommandGroup
from app.bot.message_handler import IncomingMessage, MessageHandler, SensitiveNotice
from app.bot.reminder_commands import ReminderCommandGroup, TimezoneCommandGroup
from app.bot.reminder_sender import DiscordReminderSender
from app.bot.trial_commands import TrialCommandGroup
from app.config import Settings
from app.conversations.context_builder import ContextBuilder
from app.conversations.history_retriever import HistoricalContextRetriever
from app.conversations.segmenter import ConversationSegmenter
from app.health import remove_heartbeat, write_heartbeat
from app.memory.personal_memory import MemoryCaptureOutcome, PersonalMemoryService
from app.reminders.dispatcher import ReminderDispatcher
from app.reminders.service import ReminderService
from app.security.sensitive_filter import SensitiveFilter
from app.storage.admin_audit import AdminAuditRepository
from app.storage.background_memory import BackgroundMemoryRepository
from app.storage.memory_groups import ChannelAccessRepository
from app.storage.reminders import ReminderRepository
from app.storage.repositories import MessageRepository, NewMessage
from app.storage.trial import TrialRepository
from app.vision.discord_sources import extract_discord_visuals
from app.workers.background_worker import BackgroundWorker

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
        persona: Persona,
        background_repository: BackgroundMemoryRepository | None = None,
        background_worker: BackgroundWorker | None = None,
        history_retriever: HistoricalContextRetriever | None = None,
        personal_memory_service: PersonalMemoryService | None = None,
        reminder_service: ReminderService | None = None,
        reminder_repository: ReminderRepository | None = None,
        admin_audit_repository: AdminAuditRepository | None = None,
        trial_repository: TrialRepository | None = None,
        channel_access_repository: ChannelAccessRepository | None = None,
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
        self._background_repository = background_repository
        self._background_worker = background_worker
        self._history_retriever = history_retriever
        self._personal_memory_service = personal_memory_service
        self._reminder_repository = reminder_repository
        self._admin_audit_repository = admin_audit_repository
        self._trial_repository = trial_repository
        self._channel_access_repository = channel_access_repository
        self._reminder_dispatcher = (
            ReminderDispatcher(
                repository=reminder_repository,
                sender=DiscordReminderSender(self),
                stale_after=timedelta(minutes=settings.reminder_stale_minutes),
                retry_base_delay=timedelta(seconds=settings.reminder_retry_base_seconds),
                maximum_per_run=settings.reminder_max_per_run,
            )
            if reminder_repository is not None
            else None
        )
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
            access_repository=channel_access_repository,
        )
        self.tree = discord.app_commands.CommandTree(self)
        self.tree.add_command(
            SaltGlobalCommandGroup(
                client=self,
                persona=persona,
                allowed_guild_ids=settings.allowed_guild_ids,
            )
        )
        guilds = [discord.Object(id=guild_id) for guild_id in settings.allowed_guild_ids]
        if personal_memory_service is not None:
            self.tree.add_command(
                PersonalMemoryCommandGroup(
                    service=personal_memory_service,
                    allowed_guild_ids=settings.allowed_guild_ids,
                    admin_user_ids=settings.sensitive_notification_user_ids,
                    audit_repository=admin_audit_repository,
                ),
                guilds=guilds,
            )
        if reminder_service is not None:
            self.tree.add_command(
                ReminderCommandGroup(
                    service=reminder_service,
                    allowed_guild_ids=settings.allowed_guild_ids,
                ),
                guilds=guilds,
            )
            self.tree.add_command(
                TimezoneCommandGroup(
                    service=reminder_service,
                    allowed_guild_ids=settings.allowed_guild_ids,
                ),
                guilds=guilds,
            )
        if (
            reminder_repository is not None
            and admin_audit_repository is not None
            and background_repository is not None
            and trial_repository is not None
        ):
            self.tree.add_command(
                BotAdminCommandGroup(
                    client=self,
                    budget_manager=budget_manager,
                    background_repository=background_repository,
                    reminder_repository=reminder_repository,
                    audit_repository=admin_audit_repository,
                    allowed_guild_ids=settings.allowed_guild_ids,
                    admin_user_ids=settings.sensitive_notification_user_ids,
                    trial_repository=trial_repository,
                ),
                guilds=guilds,
            )
        if channel_access_repository is not None and admin_audit_repository is not None:
            self.tree.add_command(
                AdminMemoryCommandGroup(
                    repository=channel_access_repository,
                    audit_repository=admin_audit_repository,
                    allowed_guild_ids=settings.allowed_guild_ids,
                    admin_user_ids=settings.sensitive_notification_user_ids,
                ),
                guilds=guilds,
            )
        if trial_repository is not None and admin_audit_repository is not None:
            self.tree.add_command(
                TrialCommandGroup(
                    repository=trial_repository,
                    audit_repository=admin_audit_repository,
                    allowed_guild_ids=settings.allowed_guild_ids,
                    admin_user_ids=settings.sensitive_notification_user_ids,
                ),
                guilds=guilds,
            )

    async def setup_hook(self) -> None:
        """補處理重啟前未切段訊息，並啟動定期封存檢查。"""

        recovered = await self._segmenter.assign_pending_messages()
        if recovered:
            LOGGER.info("已補處理未切段訊息 count=%s", recovered)
        await self._sync_application_commands()
        self.archive_inactive_segments.start()
        self.dispatch_budget_notifications.start()
        self.write_health_heartbeat.change_interval(
            seconds=self._settings.health_heartbeat_interval_seconds
        )
        self.write_health_heartbeat.start()
        if self._settings.background_ai_enabled and self._background_worker is not None:
            self.process_background_jobs.change_interval(
                minutes=self._settings.background_job_interval_minutes
            )
            self.process_background_jobs.start()
        if self._reminder_dispatcher is not None:
            self.dispatch_reminders.change_interval(
                seconds=self._settings.reminder_dispatch_interval_seconds
            )
            self.dispatch_reminders.start()

    async def _sync_application_commands(self) -> None:
        """全域只同步公開指令，管理及個人功能只同步到白名單伺服器。"""

        try:
            global_commands = await self.tree.sync()
        except discord.DiscordException as error:
            LOGGER.error(
                "全域 Slash Command 同步失敗 error_type=%s http_status=%s code=%s",
                type(error).__name__,
                getattr(error, "status", None),
                getattr(error, "code", None),
            )
        else:
            LOGGER.info(
                "全域 Slash Command 同步完成 count=%s names=%s",
                len(global_commands),
                ",".join(command.name for command in global_commands),
            )
        for guild_id in self._settings.allowed_guild_ids:
            guild = discord.Object(id=guild_id)
            try:
                commands = await self.tree.sync(guild=guild)
            except discord.DiscordException as error:
                LOGGER.error(
                    "應用程式 Slash Command 同步失敗 guild_id=%s "
                    "error_type=%s http_status=%s code=%s",
                    guild_id,
                    type(error).__name__,
                    getattr(error, "status", None),
                    getattr(error, "code", None),
                )
            else:
                LOGGER.info(
                    "應用程式 Slash Command 同步完成 guild_id=%s count=%s names=%s",
                    guild_id,
                    len(commands),
                    ",".join(command.name for command in commands),
                )

    async def close(self) -> None:
        """停止定期封存工作並關閉 Discord 連線。"""

        await self._companion_scheduler.close()
        if self.archive_inactive_segments.is_running():
            self.archive_inactive_segments.cancel()
        if self.dispatch_budget_notifications.is_running():
            self.dispatch_budget_notifications.cancel()
        if self.process_background_jobs.is_running():
            self.process_background_jobs.cancel()
        if self.dispatch_reminders.is_running():
            self.dispatch_reminders.cancel()
        if self.write_health_heartbeat.is_running():
            self.write_health_heartbeat.cancel()
            remove_heartbeat(self._settings.health_heartbeat_path)
        await super().close()

    @tasks.loop(minutes=1)
    async def archive_inactive_segments(self) -> None:
        """每分鐘封存滿 30 分鐘沒有新訊息的段落。"""

        try:
            archived_ids = await self._segmenter.archive_inactive_segment_ids()
            if (
                archived_ids
                and self._settings.background_ai_enabled
                and self._background_repository is not None
            ):
                created = await self._background_repository.enqueue_archived_segments(
                    archived_ids,
                    max_attempts=self._settings.background_job_max_attempts,
                )
                if created:
                    LOGGER.info("已排入新封存段落摘要工作 count=%s", created)
                if (
                    self._background_worker is not None
                    and await self._background_repository.pending_count()
                    >= self._settings.background_job_high_water_mark
                ):
                    await self._run_background_jobs()
        except Exception as error:
            LOGGER.error("段落封存檢查失敗 error_type=%s", type(error).__name__)

    @tasks.loop(minutes=5)
    async def process_background_jobs(self) -> None:
        """依設定間隔執行一批持久化摘要與向量工作。"""

        await self._run_background_jobs()

    async def _run_background_jobs(self) -> None:
        """執行背景工作並只記錄不含訊息內容的結果。"""

        if self._background_worker is None:
            return
        try:
            result = await self._background_worker.run_once()
            if any((result.completed, result.deferred, result.retried, result.failed)):
                LOGGER.info(
                    "背景工作批次完成 completed=%s deferred=%s retried=%s failed=%s",
                    result.completed,
                    result.deferred,
                    result.retried,
                    result.failed,
                )
        except Exception as error:
            LOGGER.error("背景工作批次失敗 error_type=%s", type(error).__name__)

    @tasks.loop(minutes=1)
    async def dispatch_budget_notifications(self) -> None:
        """每分鐘傳送尚未完成的 70%／90% 預算通知。"""

        try:
            await self._budget_manager.dispatch_pending_notifications(self._budget_notifier)
        except Exception as error:
            LOGGER.error("預算門檻通知失敗 error_type=%s", type(error).__name__)

    @tasks.loop(seconds=30)
    async def dispatch_reminders(self) -> None:
        """定期派送到期私訊提醒，不呼叫 AI，也不公開補發。"""

        if self._reminder_dispatcher is None:
            return
        try:
            result = await self._reminder_dispatcher.run_once()
            if any((result.sent, result.retried, result.failed)):
                LOGGER.info(
                    "提醒派送批次完成 sent=%s retried=%s failed=%s",
                    result.sent,
                    result.retried,
                    result.failed,
                )
        except Exception as error:
            LOGGER.error("提醒派送批次失敗 error_type=%s", type(error).__name__)

    @tasks.loop(seconds=15)
    async def write_health_heartbeat(self) -> None:
        """只在 Discord 用戶端就緒時更新本機健康心跳。"""

        if self.is_ready():
            write_heartbeat(self._settings.health_heartbeat_path)

    async def on_ready(self) -> None:
        """記錄連線完成；日誌只包含必要 ID。"""

        write_heartbeat(self._settings.health_heartbeat_path)
        if self.user is not None:
            LOGGER.info("Discord 連線完成 bot_user_id=%s", self.user.id)

    async def on_message(self, message: discord.Message) -> None:
        """接收 Discord 訊息，忽略私訊、非白名單與機器人自己的訊息。"""

        if message.type not in {discord.MessageType.default, discord.MessageType.reply}:
            return

        replied_to_message_id = None
        if message.reference is not None and message.reference.message_id is not None:
            replied_to_message_id = str(message.reference.message_id)

        visual_inputs = extract_discord_visuals(message)
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
            visual_inputs=visual_inputs,
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
        memory_outcome = await self._capture_personal_memory_safely(incoming)
        if memory_outcome is not None and memory_outcome.status in {
            "created",
            "duplicate",
            "invalid_content",
            "blocked_sensitive",
            "ambiguous_delete",
            "unsupported_memory_subject",
        }:
            await self._send_memory_event_reply(message, memory_outcome)
            return
        try:
            await self._route_ai_reply(message, incoming)
        except Exception as error:
            LOGGER.error(
                "AI 回覆流程失敗 message_id=%s error_type=%s",
                message.id,
                type(error).__name__,
            )

    async def _capture_personal_memory_safely(
        self,
        incoming: IncomingMessage,
    ) -> MemoryCaptureOutcome | None:
        """擷取失敗不得阻止既有聊天回覆流程。"""

        if self._personal_memory_service is None or incoming.guild_id is None:
            return None
        try:
            memory_outcome = await self._personal_memory_service.capture_explicit_message(
                guild_id=str(incoming.guild_id),
                user_id=str(incoming.author_id),
                message_id=incoming.discord_message_id,
                content=incoming.content,
            )
            if memory_outcome.status in {"created", "duplicate"}:
                memory_id = (
                    memory_outcome.save_result.memory.id
                    if memory_outcome.save_result is not None
                    else None
                )
                LOGGER.info(
                    "個人記憶事件完成 message_id=%s user_id=%s memory_id=%s status=%s",
                    incoming.discord_message_id,
                    incoming.author_id,
                    memory_id,
                    memory_outcome.status,
                )
            return memory_outcome
        except Exception as error:
            LOGGER.error(
                "個人記憶事件處理失敗 message_id=%s user_id=%s error_type=%s",
                incoming.discord_message_id,
                incoming.author_id,
                type(error).__name__,
            )
            return None

    async def _send_memory_event_reply(
        self,
        message: discord.Message,
        outcome: MemoryCaptureOutcome,
    ) -> None:
        """以固定免費文字確認記憶事件，避免再呼叫聊天模型。"""

        memory_id = outcome.save_result.memory.id if outcome.save_result is not None else None
        if outcome.status == "created":
            content = f"記住了喵，記憶編號是 #{memory_id}"
        elif outcome.status == "duplicate":
            content = f"這個已經記得了，編號是 #{memory_id}"
        elif outcome.status == "blocked_sensitive":
            content = "這段可能含敏感資料，Salt 不會把它存成記憶"
        elif outcome.status == "ambiguous_delete":
            content = (
                "可以喵，不過 Salt 還不知道你指的是哪一筆\n"
                "請先用 /memory view 查看編號，再使用 /memory delete"
            )
        elif outcome.status == "unsupported_memory_subject":
            content = (
                "這比較像群組稱號或別人的資料喵，目前 Salt 只能保存你自己的個人資料，"
                "所以這次沒有存進記憶"
            )
        else:
            content = "這段記憶太長或格式不完整，請改用 /memory set"
        try:
            await message.reply(
                content,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.DiscordException as error:
            LOGGER.error(
                "個人記憶確認回覆失敗 message_id=%s error_type=%s",
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
            has_visual=(
                self._settings.ai_vision_enabled and incoming.has_processable_visual_candidate
            ),
            now=datetime.now(UTC),
        )
        explicit_decision = self._trigger_policy.decide(
            ChannelMode.NORMAL,
            explicit_signals,
        )
        await self._record_trial_event(
            idempotency_key=f"explicit_check:{incoming.discord_message_id}",
            event_type="reply_decision",
            incoming=incoming,
            channel_mode=mode.value,
            trigger_kind=explicit_decision.kind.value,
            reason=explicit_decision.reason,
            outcome="reply" if explicit_decision.should_reply else "no_reply",
        )
        if explicit_decision.should_reply:
            self._companion_scheduler.cancel(incoming.channel_id)
            await self._send_ai_reply(
                message,
                incoming,
                companion_generated=False,
                trigger_kind=explicit_decision.kind.value,
            )
            return
        if explicit_decision.reason == "slash_like_text":
            self._companion_scheduler.cancel(incoming.channel_id)
            return
        if mode is ChannelMode.COMPANION:
            await self._record_trial_event(
                idempotency_key=f"companion_schedule:{incoming.discord_message_id}",
                event_type="companion_schedule",
                incoming=incoming,
                channel_mode=mode.value,
                reason="observation_window",
                outcome="scheduled",
            )
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
        human_cutoff = now - timedelta(seconds=self._settings.companion_activity_window_seconds)
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
                has_visual=(
                    self._settings.ai_vision_enabled and incoming.has_processable_visual_candidate
                ),
                recent_human_author_ids=human_ids,
                bot_spoke_recently=bot_spoke_recently,
                last_companion_reply_at=self._last_companion_reply_at.get(incoming.channel_id),
                now=now,
            ),
        )
        await self._record_trial_event(
            idempotency_key=f"companion_decision:{incoming.discord_message_id}",
            event_type="reply_decision",
            incoming=incoming,
            channel_mode=ChannelMode.COMPANION.value,
            trigger_kind=decision.kind.value,
            reason=decision.reason,
            outcome="reply" if decision.should_reply else "no_reply",
        )
        if decision.should_reply:
            if self._trial_repository is not None:
                slot = await self._trial_repository.reserve_companion_reply(
                    guild_id=str(incoming.guild_id),
                    channel_id=str(incoming.channel_id),
                    message_id=incoming.discord_message_id,
                    now=now,
                )
                if slot in {"inactive", "daily_limit", "outside_scope"}:
                    await self._record_trial_event(
                        idempotency_key=(f"companion_limit:{incoming.discord_message_id}"),
                        event_type="reply_blocked",
                        incoming=incoming,
                        channel_mode=ChannelMode.COMPANION.value,
                        trigger_kind=decision.kind.value,
                        reason=slot,
                        outcome="no_reply",
                    )
                    return
            await self._send_ai_reply(
                message,
                incoming,
                companion_generated=True,
                trigger_kind=decision.kind.value,
                reply_to_message=decision.reason == "question_or_help_request",
            )

    async def _send_ai_reply(
        self,
        message: discord.Message,
        incoming: IncomingMessage,
        *,
        companion_generated: bool,
        trigger_kind: str | None = None,
        reply_to_message: bool = True,
    ) -> None:
        """建立上下文並依回答或加入話題模式傳送及保存訊息。"""

        if self.user is None:
            return
        started_at = perf_counter()
        context = await self._context_builder.build(
            incoming.discord_message_id,
            assistant_author_id=str(self.user.id),
        )
        if self._settings.background_ai_enabled and self._history_retriever is not None:
            summaries = await self._history_retriever.retrieve(
                trigger_message_id=incoming.discord_message_id,
                query_text=incoming.content,
            )
            context = self._context_builder.add_historical_summaries(
                context,
                summaries,
                maximum_characters=self._settings.ai_history_context_characters,
            )
        outcome = await self._chat_service.generate(
            context,
            visual_inputs=incoming.visual_inputs,
            trigger_has_text=incoming.has_meaningful_text,
        )
        outgoing_content = self._fit_discord_message(outcome.content)
        delivery_mode = "reply" if reply_to_message else "channel"
        try:
            if reply_to_message:
                sent = await message.reply(
                    outgoing_content,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                sent = await message.channel.send(
                    outgoing_content,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except Exception as error:
            LOGGER.error(
                "Discord AI 回覆傳送失敗 trigger_message_id=%s status=%s error_type=%s",
                incoming.discord_message_id,
                outcome.status,
                type(error).__name__,
            )
            await self._record_trial_event(
                idempotency_key=f"reply_result:{incoming.discord_message_id}",
                event_type="reply_result",
                incoming=incoming,
                channel_mode=("companion" if companion_generated else "normal"),
                trigger_kind=trigger_kind,
                reason="discord_send_failed",
                outcome=outcome.status,
                latency_ms=round((perf_counter() - started_at) * 1_000),
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
                    replied_to_message_id=(
                        incoming.discord_message_id if reply_to_message else None
                    ),
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
            await self._record_trial_event(
                idempotency_key=f"reply_result:{incoming.discord_message_id}",
                event_type="reply_result",
                incoming=incoming,
                channel_mode=("companion" if companion_generated else "normal"),
                trigger_kind=trigger_kind,
                reason="reply_save_failed",
                outcome=outcome.status,
                latency_ms=round((perf_counter() - started_at) * 1_000),
            )
            return

        if companion_generated:
            self._last_companion_reply_at[incoming.channel_id] = datetime.now(UTC)
        LOGGER.info(
            "Discord AI 回覆完成 trigger_message_id=%s reply_message_id=%s "
            "status=%s delivery_mode=%s",
            incoming.discord_message_id,
            sent.id,
            outcome.status,
            delivery_mode,
        )
        await self._record_trial_event(
            idempotency_key=f"reply_result:{incoming.discord_message_id}",
            event_type="reply_result",
            incoming=incoming,
            channel_mode=("companion" if companion_generated else "normal"),
            trigger_kind=trigger_kind,
            reason=("discord_reply_saved" if reply_to_message else "discord_channel_message_saved"),
            outcome=outcome.status,
            latency_ms=round((perf_counter() - started_at) * 1_000),
        )

    async def _record_trial_event(
        self,
        *,
        idempotency_key: str,
        event_type: str,
        incoming: IncomingMessage,
        channel_mode: str | None = None,
        trigger_kind: str | None = None,
        reason: str | None = None,
        outcome: str | None = None,
        latency_ms: int | None = None,
    ) -> None:
        """觀測失敗不得影響訊息保存或 Discord 回覆。"""

        if self._trial_repository is None:
            return
        try:
            await self._trial_repository.record_event(
                idempotency_key=idempotency_key,
                event_type=event_type,
                guild_id=str(incoming.guild_id) if incoming.guild_id is not None else None,
                channel_id=str(incoming.channel_id),
                message_id=incoming.discord_message_id,
                channel_mode=channel_mode,
                trigger_kind=trigger_kind,
                reason=reason,
                outcome=outcome,
                latency_ms=latency_ms,
            )
        except Exception as error:
            LOGGER.error(
                "試跑觀測事件保存失敗 message_id=%s error_type=%s",
                incoming.discord_message_id,
                type(error).__name__,
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
