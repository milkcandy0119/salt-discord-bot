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
    assert revision == ("20260804_0004",)


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
