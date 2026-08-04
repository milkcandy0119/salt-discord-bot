from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.health import HealthCheckError, remove_heartbeat, run_health_check, write_heartbeat
from app.storage.database import upgrade_database


def test_health_check_accepts_fresh_heartbeat_and_migrated_sqlite(
    temporary_test_directory: Path,
) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    heartbeat = temporary_test_directory / "runtime" / "heartbeat"
    database_path = temporary_test_directory / "data" / "assistant.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    upgrade_database(database_url)
    write_heartbeat(heartbeat, now=now)

    run_health_check(
        database_url=database_url,
        heartbeat_path=heartbeat,
        max_age_seconds=90,
        now=now + timedelta(seconds=30),
    )


def test_health_check_rejects_stale_or_removed_heartbeat(
    migrated_database_url: str,
    temporary_test_directory: Path,
) -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
    heartbeat = temporary_test_directory / "heartbeat"
    write_heartbeat(heartbeat, now=now - timedelta(seconds=91))

    with pytest.raises(HealthCheckError, match="過期"):
        run_health_check(
            database_url=migrated_database_url,
            heartbeat_path=heartbeat,
            max_age_seconds=90,
            now=now,
        )

    remove_heartbeat(heartbeat)
    with pytest.raises(HealthCheckError, match="找不到"):
        run_health_check(
            database_url=migrated_database_url,
            heartbeat_path=heartbeat,
            max_age_seconds=90,
            now=now,
        )
