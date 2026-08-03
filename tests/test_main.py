import logging

from app.config import Settings
from app.main import run


def test_run_stays_in_safe_mode_and_reports_missing_setting_names(caplog) -> None:
    caplog.set_level(logging.INFO)

    exit_code = run(Settings(_env_file=None))

    assert exit_code == 0
    assert "外部整合停用" in caplog.text
    assert "DISCORD_BOT_TOKEN" in caplog.text
    assert "OpenAI 整合未設定" in caplog.text


def test_run_never_logs_secret_values(caplog) -> None:
    caplog.set_level(logging.INFO)
    raw_secret = "do-not-log-this-secret"
    settings = Settings(
        _env_file=None,
        discord_bot_token=raw_secret,
        discord_allowed_guild_ids="123",
        discord_allowed_channel_ids="456",
        discord_owner_user_id="789",
        openai_api_key=raw_secret,
    )

    assert run(settings) == 0
    assert raw_secret not in caplog.text
