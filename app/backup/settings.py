"""只供備份容器使用且不承載密碼內容的設定。"""

from __future__ import annotations

from datetime import time

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BackupSettings(BaseSettings):
    """Restic 只接收密碼檔路徑，密碼本身不會進入設定模型。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: str = "sqlite+aiosqlite:///data/discord_assistant.db"
    restic_repository: str = "/backups/restic"
    restic_password_file: str = "/run/secrets/restic_password"
    restic_binary: str = "restic"
    backup_daily_time_utc: str = "19:00"
    backup_keep_last: int = Field(default=7, ge=1, le=365)
    backup_command_timeout_seconds: int = Field(default=3_600, ge=30, le=86_400)

    @field_validator(
        "database_url",
        "restic_repository",
        "restic_password_file",
        "restic_binary",
        mode="before",
    )
    @classmethod
    def required_text_is_not_blank(cls, value: object) -> object:
        """拒絕可能讓備份落到未知位置的空白設定。"""

        if isinstance(value, str) and value.strip():
            return value.strip()
        raise ValueError("備份路徑與命令設定不得為空白")

    @field_validator("backup_daily_time_utc")
    @classmethod
    def valid_daily_time(cls, value: str) -> str:
        """每日排程固定採 UTC 的 24 小時 HH:MM 格式。"""

        try:
            time.fromisoformat(value)
        except ValueError as error:
            raise ValueError("BACKUP_DAILY_TIME_UTC 必須使用 HH:MM") from error
        if len(value) != 5:
            raise ValueError("BACKUP_DAILY_TIME_UTC 必須使用 HH:MM")
        return value

    @property
    def daily_time(self) -> time:
        """傳回已驗證的每日 UTC 執行時間。"""

        return time.fromisoformat(self.backup_daily_time_utc)
