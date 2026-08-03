"""階段 0 的可執行進入點。"""

from __future__ import annotations

import asyncio
import logging

from app.bot.client import DiscordAssistantClient
from app.config import Settings, get_settings
from app.logging_config import configure_logging
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
        LOGGER.info("OpenAI 整合未設定且保持停用")
    else:
        LOGGER.info("已偵測 OpenAI 憑證，但階段 1 禁止任何付費 AI 呼叫")

    return 0


async def run_discord(settings: Settings) -> int:
    """套用 migration 後啟動 Discord 用戶端。"""

    await asyncio.to_thread(upgrade_database, settings.database_url)
    database = Database(settings.database_url)
    repository = MessageRepository(database.session_factory)
    client = DiscordAssistantClient(settings=settings, repository=repository)
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
