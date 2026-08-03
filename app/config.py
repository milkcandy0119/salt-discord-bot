"""由環境變數提供的應用程式設定。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """從環境變數與選用的本機 .env 載入設定。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    discord_bot_token: SecretStr | None = None
    discord_allowed_guild_ids: str = ""
    discord_allowed_channel_ids: str = ""
    discord_owner_user_id: str = ""
    discord_admin_user_ids: str = ""
    database_url: str = "sqlite+aiosqlite:///data/discord_assistant.db"
    openai_api_key: SecretStr | None = None

    @field_validator("discord_bot_token", "openai_api_key", mode="before")
    @classmethod
    def empty_secret_is_missing(cls, value: object) -> object:
        """將空白的祕密環境變數視為未提供選用設定。"""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: object) -> object:
        """接受不區分大小寫的標準日誌層級。"""

        return value.upper() if isinstance(value, str) else value

    @property
    def missing_discord_settings(self) -> tuple[str, ...]:
        """傳回後續 Discord 整合仍缺少的設定名稱。"""

        missing: list[str] = []
        if self.discord_bot_token is None:
            missing.append("DISCORD_BOT_TOKEN")
        if not self.discord_allowed_guild_ids.strip():
            missing.append("DISCORD_ALLOWED_GUILD_IDS")
        if not self.discord_allowed_channel_ids.strip():
            missing.append("DISCORD_ALLOWED_CHANNEL_IDS")
        if not self.discord_owner_user_id.strip():
            missing.append("DISCORD_OWNER_USER_ID")
        return tuple(missing)

    @staticmethod
    def _parse_discord_ids(raw_value: str, setting_name: str) -> frozenset[int]:
        """解析逗號分隔的 Discord snowflake ID，拒絕不合法設定。"""

        values = [item.strip() for item in raw_value.split(",") if item.strip()]
        if any(not item.isdecimal() for item in values):
            raise ValueError(f"{setting_name} 必須是逗號分隔的正整數")
        parsed = frozenset(int(item) for item in values)
        if any(item <= 0 for item in parsed):
            raise ValueError(f"{setting_name} 必須是逗號分隔的正整數")
        return parsed

    @property
    def allowed_guild_ids(self) -> frozenset[int]:
        """傳回允許接收訊息的 Discord 伺服器 ID。"""

        return self._parse_discord_ids(
            self.discord_allowed_guild_ids,
            "DISCORD_ALLOWED_GUILD_IDS",
        )

    @property
    def allowed_channel_ids(self) -> frozenset[int]:
        """傳回允許接收訊息的 Discord 頻道 ID。"""

        return self._parse_discord_ids(
            self.discord_allowed_channel_ids,
            "DISCORD_ALLOWED_CHANNEL_IDS",
        )

    @property
    def owner_user_id(self) -> int:
        """傳回具有敏感事件稽核權限的機器人擁有者 ID。"""

        values = self._parse_discord_ids(self.discord_owner_user_id, "DISCORD_OWNER_USER_ID")
        if len(values) != 1:
            raise ValueError("DISCORD_OWNER_USER_ID 必須剛好包含一個正整數")
        return next(iter(values))

    @property
    def admin_user_ids(self) -> frozenset[int]:
        """傳回具有敏感事件稽核權限的管理員 ID。"""

        return self._parse_discord_ids(self.discord_admin_user_ids, "DISCORD_ADMIN_USER_IDS")

    @property
    def sensitive_notification_user_ids(self) -> frozenset[int]:
        """傳回需要接收敏感事件通知的擁有者與管理員 ID。"""

        return self.admin_user_ids | {self.owner_user_id}


@lru_cache
def get_settings() -> Settings:
    """載入並快取目前程序的設定。"""

    return Settings()
