from pydantic import SecretStr

from app.config import Settings


def test_defaults_are_safe_without_credentials() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.openai_api_key is None
    assert settings.missing_discord_settings == (
        "DISCORD_BOT_TOKEN",
        "DISCORD_ALLOWED_GUILD_IDS",
        "DISCORD_ALLOWED_CHANNEL_IDS",
        "DISCORD_OWNER_USER_ID",
    )


def test_secret_values_are_redacted_in_settings_representation() -> None:
    raw_secret = "do-not-log-this-secret"
    settings = Settings(
        _env_file=None,
        discord_bot_token=raw_secret,
        openai_api_key=raw_secret,
    )

    assert isinstance(settings.discord_bot_token, SecretStr)
    assert raw_secret not in repr(settings)
    assert str(settings.discord_bot_token) == "**********"


def test_blank_secrets_are_treated_as_missing() -> None:
    settings = Settings(_env_file=None, discord_bot_token="  ", openai_api_key="")

    assert settings.discord_bot_token is None
    assert settings.openai_api_key is None


def test_discord_id_lists_are_parsed_without_duplicates() -> None:
    settings = Settings(
        _env_file=None,
        discord_allowed_guild_ids="1, 2,1",
        discord_allowed_channel_ids="3,4",
        discord_owner_user_id="5",
        discord_admin_user_ids="6, 7",
    )

    assert settings.allowed_guild_ids == frozenset({1, 2})
    assert settings.allowed_channel_ids == frozenset({3, 4})
    assert settings.sensitive_notification_user_ids == frozenset({5, 6, 7})
