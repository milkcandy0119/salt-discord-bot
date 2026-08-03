"""Discord 助手的設定檢查與執行進入點。"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from app.ai.budget_manager import BudgetManager, ModelPrice
from app.ai.chat_service import ChatService, OpenAIResponsesProvider
from app.ai.persona import load_persona
from app.bot.client import DiscordAssistantClient
from app.config import Settings, get_settings
from app.conversations.context_builder import ContextBuilder
from app.conversations.segmenter import ConversationSegmenter
from app.logging_config import configure_logging
from app.security.sensitive_filter import SensitiveFilter
from app.storage.database import Database, upgrade_database
from app.storage.repositories import MessageRepository

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
    )
    segmenter = ConversationSegmenter(
        database.session_factory,
        implicit_continuation_window=timedelta(
            minutes=settings.conversation_implicit_continuation_minutes
        ),
    )
    client = DiscordAssistantClient(
        settings=settings,
        repository=repository,
        segmenter=segmenter,
        budget_manager=budget_manager,
        context_builder=context_builder,
        chat_service=chat_service,
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
