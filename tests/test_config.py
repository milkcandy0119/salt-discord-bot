import pytest
from pydantic import SecretStr, ValidationError

from app.config import Settings


def test_defaults_are_safe_without_credentials() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_env == "development"
    assert settings.openai_api_key is None
    assert settings.companion_channel_ids == frozenset()
    assert settings.companion_observation_seconds == 5
    assert settings.companion_cooldown_seconds == 120
    assert settings.ai_chat_model == "gpt-5.6-luna"
    assert settings.ai_recent_participant_context_minutes == 5
    assert settings.ai_recent_messages_per_participant == 4
    assert settings.ai_recent_participant_context_characters == 2_000
    assert settings.ai_max_mentioned_participants == 3
    assert settings.ai_personal_memory_context_characters == 1_500
    assert settings.ai_vision_enabled is False
    assert settings.ai_vision_max_images_per_message == 1
    assert settings.ai_vision_max_download_bytes == 8 * 1_024 * 1_024
    assert settings.ai_vision_max_pixels == 20_000_000
    assert settings.ai_vision_download_timeout_seconds == 10
    assert settings.ai_vision_detail == "low"
    assert settings.ai_vision_max_reserved_tokens_per_image == 1_200
    assert settings.ai_vision_max_animations_per_message == 1
    assert settings.ai_vision_max_frames_per_animation == 4
    assert settings.ai_vision_max_animation_frames == 300
    assert settings.ai_vision_max_animation_total_pixels == 80_000_000
    assert settings.ai_vision_animation_processing_timeout_seconds == 3
    assert settings.ai_vision_max_animation_duration_seconds == 30
    assert settings.ai_vision_animation_duplicate_threshold == 3
    assert settings.background_ai_enabled is False
    assert settings.ai_summary_model == "gpt-5.4-nano-2026-03-17"
    assert settings.ai_summary_max_output_tokens == 300
    assert settings.ai_embedding_model == "text-embedding-3-small"
    assert settings.ai_embedding_dimensions == 1_536
    assert settings.ai_history_result_limit == 3
    assert settings.background_job_interval_minutes == 5
    assert settings.reminder_default_timezone == "Asia/Taipei"
    assert settings.reminder_dispatch_interval_seconds == 30
    assert settings.reminder_max_per_run == 20
    assert settings.reminder_max_attempts == 5
    assert settings.health_heartbeat_path == "runtime/discord-assistant.heartbeat"
    assert settings.health_heartbeat_interval_seconds == 15
    assert settings.health_max_age_seconds == 90
    assert settings.trial_duration_days == 7
    assert settings.trial_timezone == "Asia/Taipei"
    assert settings.trial_global_increment_limit_microusd == 1_000_000
    assert settings.trial_background_increment_limit_microusd == 250_000
    assert settings.trial_companion_daily_reply_limit == 20
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


def test_animation_count_cannot_exceed_one_per_message() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_vision_max_animations_per_message=2)


def test_animation_count_accepts_string_value_from_environment() -> None:
    settings = Settings(
        _env_file=None,
        ai_vision_max_animations_per_message="1",  # type: ignore[arg-type]
    )

    assert settings.ai_vision_max_animations_per_message == 1


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


def test_companion_channels_must_be_allowed_by_channel_id() -> None:
    settings = Settings(
        _env_file=None,
        discord_allowed_channel_ids="3,4",
        discord_companion_channel_ids="4",
    )

    assert settings.companion_channel_ids == frozenset({4})

    invalid = Settings(
        _env_file=None,
        discord_allowed_channel_ids="3,4",
        discord_companion_channel_ids="5",
    )
    with pytest.raises(ValueError, match="子集合"):
        _ = invalid.companion_channel_ids
