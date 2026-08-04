import sqlite3
from contextlib import closing
from pathlib import Path

from app.storage.database import upgrade_database


def test_phase_one_database_upgrade_preserves_existing_messages(
    temporary_test_directory: Path,
) -> None:
    database_path = temporary_test_directory / "upgrade.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url, revision="20260803_0001")

    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO messages (
                discord_message_id, guild_id, channel_id, author_id,
                author_display_name, content, discord_created_at, received_at,
                replied_to_message_id, is_bot, is_sensitive, sensitive_categories,
                processing_status, author_notification_status, admin_notification_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "existing-message",
                "1",
                "2",
                "3",
                "既有使用者",
                "階段 1 既有訊息",
                "2026-08-03 12:00:00",
                "2026-08-03 12:00:00",
                None,
                0,
                0,
                "[]",
                "stored",
                "not_required",
                "not_required",
            ),
        )
        connection.commit()

    upgrade_database(database_url)

    with closing(sqlite3.connect(database_path)) as connection:
        message = connection.execute(
            "SELECT content, segment_id FROM messages WHERE discord_message_id = ?",
            ("existing-message",),
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert message == ("階段 1 既有訊息", None)
    assert revision == ("20260804_0008",)


def test_phase_five_migration_does_not_queue_existing_archived_segments(
    temporary_test_directory: Path,
) -> None:
    """升級只建立結構，不得把舊封存內容送入付費佇列。"""

    database_path = temporary_test_directory / "phase-five-upgrade.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url, revision="20260803_0003")
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO conversation_segments (
                id, guild_id, channel_id, root_message_id, status,
                created_at, last_message_at, archived_at, reopened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "1",
                "2",
                "old-root",
                "archived",
                "2026-08-01 12:00:00",
                "2026-08-01 12:01:00",
                "2026-08-01 12:31:00",
                None,
            ),
        )
        connection.commit()

    upgrade_database(database_url)

    with closing(sqlite3.connect(database_path)) as connection:
        job_count = connection.execute("SELECT COUNT(*) FROM background_jobs").fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert job_count == (0,)
    assert {"background_jobs", "segment_summaries", "summary_embeddings"} <= tables


def test_personal_memory_migration_does_not_infer_existing_chat(
    temporary_test_directory: Path,
) -> None:
    """升級只建立記憶結構，不得從既有聊天自動推測個人資料。"""

    database_path = temporary_test_directory / "personal-memory-upgrade.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url, revision="20260804_0004")

    upgrade_database(database_url)

    with closing(sqlite3.connect(database_path)) as connection:
        memory_count = connection.execute(
            "SELECT COUNT(*) FROM personal_memories"
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert memory_count == (0,)
    assert revision == ("20260804_0008",)


def test_reminder_migration_creates_no_default_jobs_or_private_data(
    temporary_test_directory: Path,
) -> None:
    """升級只建立結構，不得擅自建立提醒、時區或稽核事件。"""

    database_path = temporary_test_directory / "reminder-upgrade.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url, revision="20260804_0005")

    upgrade_database(database_url)

    with closing(sqlite3.connect(database_path)) as connection:
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("reminders", "user_timezones", "admin_audit_events")
        )
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert counts == (0, 0, 0)
    assert revision == ("20260804_0008",)


def test_trial_migration_does_not_start_or_infer_a_trial(
    temporary_test_directory: Path,
) -> None:
    """升級只建立觀測結構，不得自動開始計時或建立評價。"""

    database_path = temporary_test_directory / "trial-upgrade.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url, revision="20260804_0006")

    upgrade_database(database_url)

    with closing(sqlite3.connect(database_path)) as connection:
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "trial_sessions",
                "trial_events",
                "trial_daily_counters",
                "trial_feedback",
            )
        )
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert counts == (0, 0, 0, 0)
    assert revision == ("20260804_0008",)


def test_production_migration_preserves_existing_trial_and_adds_safe_state(
    temporary_test_directory: Path,
) -> None:
    """升級正式運行狀態時不得重設既有試跑基準。"""

    database_path = temporary_test_directory / "production-upgrade.sqlite3"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url, revision="20260804_0007")
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            """
            INSERT INTO trial_sessions (
                status, guild_ids, channel_ids, companion_channel_ids, timezone_name,
                baseline_global_committed_microusd,
                baseline_background_committed_microusd,
                global_increment_limit_microusd,
                background_increment_limit_microusd,
                companion_daily_reply_limit,
                started_at, ends_at, ended_at, stopped_reason, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "active",
                '["1"]',
                '["2"]',
                '["2"]',
                "Asia/Taipei",
                0,
                0,
                1_000_000,
                250_000,
                20,
                "2026-08-04 00:00:00",
                "2026-08-11 00:00:00",
                None,
                None,
                "2026-08-04 00:00:00",
                "2026-08-04 00:00:00",
            ),
        )
        connection.commit()

    upgrade_database(database_url)

    with closing(sqlite3.connect(database_path)) as connection:
        row = connection.execute(
            """
            SELECT status, final_global_increment_microusd,
                   final_background_increment_microusd
            FROM trial_sessions
            """
        ).fetchone()
        connection.execute("UPDATE trial_sessions SET status = 'production'")
        connection.commit()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert row == ("active", None, None)
    assert revision == ("20260804_0008",)
