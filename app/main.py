"""Discord 助手的設定檢查與執行進入點。"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from app.ai.budget_manager import BudgetManager, ModelPrice
from app.ai.chat_service import ChatService, OpenAIResponsesProvider
from app.ai.embedding_service import EmbeddingService, OpenAIEmbeddingProvider
from app.ai.persona import load_persona
from app.ai.summary_service import OpenAISummaryProvider, SummaryService
from app.bot.client import DiscordAssistantClient
from app.config import Settings, get_settings
from app.conversations.context_builder import ContextBuilder
from app.conversations.history_retriever import HistoricalContextRetriever
from app.conversations.segmenter import ConversationSegmenter
from app.logging_config import configure_logging
from app.memory.personal_memory import PersonalMemoryService
from app.reminders.service import ReminderService
from app.security.sensitive_filter import SensitiveFilter
from app.storage.admin_audit import AdminAuditRepository
from app.storage.background_memory import BackgroundMemoryRepository
from app.storage.database import Database, upgrade_database
from app.storage.personal_memories import PersonalMemoryRepository
from app.storage.reminders import ReminderRepository
from app.storage.repositories import MessageRepository
from app.storage.trial import TrialRepository
from app.storage.vector_store import SQLiteVectorStore
from app.workers.background_worker import BackgroundWorker

LOGGER = logging.getLogger(__name__)


def run(settings: Settings) -> int:
    """執行啟動前安全檢查，不建立任何外部連線。"""

    missing = settings.missing_discord_settings
    if missing:
        LOGGER.warning(
            "Discord 助手以安全模式啟動；外部整合停用，缺少設定：%s",
            ", ".join(missing),
        )
    else:
        LOGGER.info(
            "Discord 階段 1 設定驗證完成 guild_count=%s channel_count=%s "
            "notification_recipient_count=%s",
            len(settings.allowed_guild_ids),
            len(settings.allowed_channel_ids),
            len(settings.sensitive_notification_user_ids),
        )

    if settings.openai_api_key is None:
        LOGGER.info("OpenAI 整合未設定；AI 觸發時只會使用固定維護訊息")
    else:
        LOGGER.info(
            "已偵測 OpenAI 憑證；只有通過頻道觸發、安全檢查與預算預留才會呼叫模型"
        )

    return 0


async def run_discord(settings: Settings) -> int:
    """套用 migration 後啟動 Discord 用戶端。"""

    await asyncio.to_thread(upgrade_database, settings.database_url)
    database = Database(settings.database_url)
    repository = MessageRepository(database.session_factory)
    personal_memory_repository = PersonalMemoryRepository(database.session_factory)
    reminder_repository = ReminderRepository(database.session_factory)
    admin_audit_repository = AdminAuditRepository(database.session_factory)
    trial_repository = TrialRepository(database.session_factory)
    personal_memory_service = PersonalMemoryService(
        personal_memory_repository,
        sensitive_filter=SensitiveFilter(),
    )
    reminder_service = ReminderService(
        reminder_repository,
        sensitive_filter=SensitiveFilter(),
        default_timezone=settings.reminder_default_timezone,
        max_attempts=settings.reminder_max_attempts,
    )
    background_repository = BackgroundMemoryRepository(database.session_factory)
    budget_manager = BudgetManager(database.session_factory)
    persona = load_persona(settings.ai_persona_path)
    provider = (
        OpenAIResponsesProvider(settings.openai_api_key.get_secret_value())
        if settings.openai_api_key is not None
        else None
    )
    chat_service = ChatService(
        provider=provider,
        budget_manager=budget_manager,
        price=ModelPrice(
            model_name=settings.ai_chat_model,
            price_version=settings.ai_chat_price_version,
            input_microusd_per_million_tokens=(
                settings.ai_chat_input_microusd_per_million_tokens
            ),
            output_microusd_per_million_tokens=(
                settings.ai_chat_output_microusd_per_million_tokens
            ),
        ),
        persona=persona,
        sensitive_filter=SensitiveFilter(),
        maintenance_message=settings.ai_maintenance_message,
        maximum_output_tokens=settings.ai_chat_max_output_tokens,
        reasoning_effort=settings.ai_chat_reasoning_effort,
    )
    context_builder = ContextBuilder(
        database.session_factory,
        maximum_characters=settings.ai_chat_max_context_characters,
        recent_participant_window=timedelta(
            minutes=settings.ai_recent_participant_context_minutes
        ),
        recent_messages_per_participant=(
            settings.ai_recent_messages_per_participant
        ),
        maximum_recent_participant_characters=(
            settings.ai_recent_participant_context_characters
        ),
        maximum_mentioned_participants=settings.ai_max_mentioned_participants,
        personal_memory_repository=personal_memory_repository,
        maximum_personal_memory_characters=(
            settings.ai_personal_memory_context_characters
        ),
    )
    segmenter = ConversationSegmenter(
        database.session_factory,
        implicit_continuation_window=timedelta(
            minutes=settings.conversation_implicit_continuation_minutes
        ),
    )
    background_worker = None
    history_retriever = None
    if settings.background_ai_enabled:
        background_provider_key = (
            settings.openai_api_key.get_secret_value()
            if settings.openai_api_key is not None
            else None
        )
        summary_provider = (
            OpenAISummaryProvider(background_provider_key)
            if background_provider_key is not None
            else None
        )
        embedding_provider = (
            OpenAIEmbeddingProvider(background_provider_key)
            if background_provider_key is not None
            else None
        )
        vector_store = SQLiteVectorStore(database.session_factory)
        embedding_service = EmbeddingService(
            provider=embedding_provider,
            repository=background_repository,
            vector_store=vector_store,
            budget_manager=budget_manager,
            price=ModelPrice(
                model_name=settings.ai_embedding_model,
                price_version=settings.ai_embedding_price_version,
                input_microusd_per_million_tokens=(
                    settings.ai_embedding_input_microusd_per_million_tokens
                ),
                output_microusd_per_million_tokens=0,
            ),
            sensitive_filter=SensitiveFilter(),
            dimensions=settings.ai_embedding_dimensions,
            chunk_characters=settings.ai_embedding_chunk_characters,
            chunk_overlap_characters=(
                settings.ai_embedding_chunk_overlap_characters
            ),
        )
        summary_service = SummaryService(
            provider=summary_provider,
            repository=background_repository,
            budget_manager=budget_manager,
            price=ModelPrice(
                model_name=settings.ai_summary_model,
                price_version=settings.ai_summary_price_version,
                input_microusd_per_million_tokens=(
                    settings.ai_summary_input_microusd_per_million_tokens
                ),
                output_microusd_per_million_tokens=(
                    settings.ai_summary_output_microusd_per_million_tokens
                ),
            ),
            sensitive_filter=SensitiveFilter(),
            maximum_output_tokens=settings.ai_summary_max_output_tokens,
            max_job_attempts=settings.background_job_max_attempts,
        )
        background_worker = BackgroundWorker(
            repository=background_repository,
            summary_service=summary_service,
            embedding_service=embedding_service,
            stale_after=timedelta(minutes=settings.background_job_stale_minutes),
            retry_base_delay=timedelta(
                seconds=settings.background_job_retry_base_seconds
            ),
            budget_retry_after=timedelta(
                minutes=settings.background_job_budget_retry_minutes
            ),
            maximum_jobs_per_run=settings.background_job_max_per_run,
        )
        history_retriever = HistoricalContextRetriever(
            database.session_factory,
            embedding_service=embedding_service,
            vector_store=vector_store,
            model_name=settings.ai_embedding_model,
            dimensions=settings.ai_embedding_dimensions,
            result_limit=settings.ai_history_result_limit,
        )
    client = DiscordAssistantClient(
        settings=settings,
        repository=repository,
        segmenter=segmenter,
        budget_manager=budget_manager,
        context_builder=context_builder,
        chat_service=chat_service,
        background_repository=background_repository,
        background_worker=background_worker,
        history_retriever=history_retriever,
        personal_memory_service=personal_memory_service,
        reminder_service=reminder_service,
        reminder_repository=reminder_repository,
        admin_audit_repository=admin_audit_repository,
        trial_repository=trial_repository,
    )
    try:
        async with client:
            token = settings.discord_bot_token
            if token is None:
                return 0
            await client.start(token.get_secret_value(), reconnect=True)
    finally:
        await database.dispose()
    return 0


def main() -> int:
    """載入設定、初始化日誌並執行應用程式。"""

    settings = get_settings()
    configure_logging(settings.log_level)
    preflight_result = run(settings)
    if settings.missing_discord_settings:
        return preflight_result
    try:
        return asyncio.run(run_discord(settings))
    except KeyboardInterrupt:
        LOGGER.info("收到中止訊號，Discord 助手已停止")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
