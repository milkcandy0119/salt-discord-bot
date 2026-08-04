"""由環境變數提供的應用程式設定。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
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
    discord_companion_channel_ids: str = ""
    discord_owner_user_id: str = ""
    discord_admin_user_ids: str = ""
    discord_ai_command_prefix: str = "!ai"
    database_url: str = "sqlite+aiosqlite:///data/discord_assistant.db"
    conversation_implicit_continuation_minutes: int = Field(default=5, ge=1, le=30)
    companion_observation_seconds: int = Field(default=5, ge=1, le=60)
    companion_cooldown_seconds: int = Field(default=120, ge=0, le=3600)
    companion_activity_window_seconds: int = Field(default=60, ge=10, le=600)
    companion_recent_bot_minutes: int = Field(default=10, ge=1, le=60)
    openai_api_key: SecretStr | None = None
    ai_persona_path: str = "personas/salt-zh-tw-v1.toml"
    ai_chat_model: str = "gpt-5.6-luna"
    ai_chat_price_version: str = "openai-2026-08-03"
    ai_chat_input_microusd_per_million_tokens: int = Field(
        default=1_000_000,
        ge=0,
    )
    ai_chat_output_microusd_per_million_tokens: int = Field(
        default=6_000_000,
        ge=0,
    )
    ai_chat_max_context_characters: int = Field(default=12_000, ge=1_000, le=100_000)
    ai_recent_participant_context_minutes: int = Field(default=5, ge=1, le=30)
    ai_recent_messages_per_participant: int = Field(default=4, ge=1, le=20)
    ai_recent_participant_context_characters: int = Field(
        default=2_000,
        ge=0,
        le=20_000,
    )
    ai_max_mentioned_participants: int = Field(default=3, ge=0, le=10)
    ai_personal_memory_context_characters: int = Field(
        default=1_500,
        ge=0,
        le=10_000,
    )
    ai_chat_max_output_tokens: int = Field(default=800, ge=1, le=16_000)
    ai_chat_reasoning_effort: Literal["none", "low", "medium"] = "low"
    ai_maintenance_message: str = "目前 AI 回覆暫時無法使用，請稍後再試。"
    ai_vision_enabled: bool = False
    ai_vision_max_images_per_message: int = Field(default=1, ge=1, le=4)
    ai_vision_max_download_bytes: int = Field(
        default=8 * 1_024 * 1_024,
        ge=1_024,
        le=25 * 1_024 * 1_024,
    )
    ai_vision_max_pixels: int = Field(default=20_000_000, ge=1, le=100_000_000)
    ai_vision_download_timeout_seconds: float = Field(default=10.0, gt=0, le=30)
    ai_vision_detail: Literal["low", "auto"] = "low"
    ai_vision_max_reserved_tokens_per_image: int = Field(
        default=1_200,
        ge=1,
        le=20_000,
    )
    ai_vision_max_dimension: int = Field(default=1_536, ge=128, le=4_096)
    ai_vision_max_animations_per_message: int = Field(default=1, ge=1, le=1)
    ai_vision_max_frames_per_animation: int = Field(default=4, ge=1, le=8)
    ai_vision_max_animation_frames: int = Field(default=300, ge=8, le=1_000)
    ai_vision_max_animation_total_pixels: int = Field(
        default=80_000_000,
        ge=1,
        le=500_000_000,
    )
    ai_vision_animation_processing_timeout_seconds: float = Field(
        default=3.0,
        gt=0,
        le=15,
    )
    ai_vision_max_animation_duration_seconds: float = Field(
        default=30.0,
        gt=0,
        le=300,
    )
    ai_vision_animation_duplicate_threshold: float = Field(
        default=3.0,
        ge=0,
        le=255,
    )
    background_ai_enabled: bool = False
    ai_summary_model: str = "gpt-5.4-nano-2026-03-17"
    ai_summary_price_version: str = "openai-2026-08-04"
    ai_summary_input_microusd_per_million_tokens: int = Field(default=200_000, ge=0)
    ai_summary_output_microusd_per_million_tokens: int = Field(default=1_250_000, ge=0)
    ai_summary_max_output_tokens: int = Field(default=300, ge=1, le=2_000)
    ai_embedding_model: str = "text-embedding-3-small"
    ai_embedding_price_version: str = "openai-2026-08-04"
    ai_embedding_input_microusd_per_million_tokens: int = Field(default=20_000, ge=0)
    ai_embedding_dimensions: int = Field(default=1_536, ge=1, le=3_072)
    ai_embedding_chunk_characters: int = Field(default=2_000, ge=100, le=20_000)
    ai_embedding_chunk_overlap_characters: int = Field(default=200, ge=0, le=5_000)
    ai_history_result_limit: int = Field(default=3, ge=0, le=20)
    ai_history_context_characters: int = Field(default=3_000, ge=0, le=20_000)
    background_job_interval_minutes: int = Field(default=5, ge=1, le=60)
    background_job_high_water_mark: int = Field(default=20, ge=1, le=10_000)
    background_job_max_per_run: int = Field(default=10, ge=1, le=1_000)
    background_job_max_attempts: int = Field(default=5, ge=1, le=20)
    background_job_retry_base_seconds: int = Field(default=60, ge=1, le=3_600)
    background_job_budget_retry_minutes: int = Field(default=5, ge=1, le=1_440)
    background_job_stale_minutes: int = Field(default=5, ge=1, le=60)
    reminder_default_timezone: str = "Asia/Taipei"
    reminder_dispatch_interval_seconds: int = Field(default=30, ge=5, le=300)
    reminder_max_per_run: int = Field(default=20, ge=1, le=1_000)
    reminder_max_attempts: int = Field(default=5, ge=1, le=20)
    reminder_retry_base_seconds: int = Field(default=60, ge=1, le=3_600)
    reminder_stale_minutes: int = Field(default=5, ge=1, le=60)
    health_heartbeat_path: str = "runtime/discord-assistant.heartbeat"
    health_heartbeat_interval_seconds: int = Field(default=15, ge=5, le=300)
    health_max_age_seconds: int = Field(default=90, ge=15, le=3_600)
    trial_duration_days: int = Field(default=7, ge=1, le=30)
    trial_timezone: str = "Asia/Taipei"
    trial_global_increment_limit_microusd: int = Field(
        default=1_000_000, ge=1, le=10_000_000
    )
    trial_background_increment_limit_microusd: int = Field(
        default=250_000, ge=1, le=3_000_000
    )
    trial_companion_daily_reply_limit: int = Field(default=20, ge=1, le=1_000)

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

    @field_validator(
        "discord_ai_command_prefix",
        "ai_persona_path",
        "ai_chat_model",
        "ai_chat_price_version",
        "ai_maintenance_message",
        "ai_summary_model",
        "ai_summary_price_version",
        "ai_embedding_model",
        "ai_embedding_price_version",
        "reminder_default_timezone",
        "health_heartbeat_path",
        "trial_timezone",
        mode="before",
    )
    @classmethod
    def required_text_is_not_blank(cls, value: object) -> object:
        """拒絕會使觸發、安全回覆或價格紀錄失去意義的空白文字。"""

        if isinstance(value, str) and value.strip():
            return value.strip()
        raise ValueError("AI 文字設定不得為空白")

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
    def companion_channel_ids(self) -> frozenset[int]:
        """傳回陪伴模式頻道，並要求它們同時位於保存白名單。"""

        companion_ids = self._parse_discord_ids(
            self.discord_companion_channel_ids,
            "DISCORD_COMPANION_CHANNEL_IDS",
        )
        unknown_ids = companion_ids - self.allowed_channel_ids
        if unknown_ids:
            raise ValueError("DISCORD_COMPANION_CHANNEL_IDS 必須是允許頻道的子集合")
        return companion_ids

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
