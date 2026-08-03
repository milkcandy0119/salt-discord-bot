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
    assert revision == ("20260803_0003",)
