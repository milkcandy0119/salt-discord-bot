import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, time
from pathlib import Path

import pytest

from app.backup.service import (
    BackupError,
    BackupService,
    create_sqlite_snapshot,
    next_daily_run,
    validate_sqlite,
)
from app.backup.settings import BackupSettings


class FakeBackupBackend:
    def __init__(self, *, restored_source: Path, fail_verification: bool = False) -> None:
        self.restored_source = restored_source
        self.fail_verification = fail_verification
        self.events: list[str] = []

    def initialize(self) -> None:
        self.events.append("initialize")

    def backup_file(self, source: Path) -> str:
        validate_sqlite(source)
        self.events.append("backup")
        return "abc12345"

    def verify_snapshot(self, snapshot_id: str) -> None:
        self.events.append(f"verify:{snapshot_id}")
        if self.fail_verification:
            raise BackupError("模擬驗證失敗")

    def mark_verified(self, snapshot_id: str) -> str:
        self.events.append(f"mark:{snapshot_id}")
        return "def67890"

    def retain_verified(self, keep_last: int) -> None:
        self.events.append(f"retain:{keep_last}")

    def restore_snapshot(self, snapshot: str, target: Path) -> str:
        self.events.append(f"restore:{snapshot}")
        shutil.copyfile(self.restored_source, target)
        return "abc12345"


def _database_path(database_url: str) -> Path:
    return Path(database_url.removeprefix("sqlite+aiosqlite:///"))


def test_sqlite_snapshot_is_consistent_and_readable(
    migrated_database_url: str,
    temporary_test_directory: Path,
) -> None:
    snapshot = temporary_test_directory / "snapshot.db"

    create_sqlite_snapshot(migrated_database_url, snapshot)
    validate_sqlite(snapshot)

    with closing(sqlite3.connect(snapshot)) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert version == ("20260804_0008",)


def test_backup_rotates_only_after_restore_verification(
    migrated_database_url: str,
) -> None:
    backend = FakeBackupBackend(restored_source=_database_path(migrated_database_url))
    service = BackupService(
        database_url=migrated_database_url,
        backend=backend,
        keep_last=7,
    )

    result = service.run_backup()

    assert result.snapshot_id == "def67890"
    assert backend.events == [
        "backup",
        "verify:abc12345",
        "mark:abc12345",
        "retain:7",
    ]


def test_failed_new_backup_never_rotates_old_backups(
    migrated_database_url: str,
) -> None:
    backend = FakeBackupBackend(
        restored_source=_database_path(migrated_database_url),
        fail_verification=True,
    )
    service = BackupService(database_url=migrated_database_url, backend=backend)

    with pytest.raises(BackupError, match="模擬驗證失敗"):
        service.run_backup()

    assert backend.events == ["backup", "verify:abc12345"]


def test_restore_is_isolated_and_never_overwrites_existing_files(
    migrated_database_url: str,
    temporary_test_directory: Path,
) -> None:
    live_database = _database_path(migrated_database_url)
    backend = FakeBackupBackend(restored_source=live_database)
    service = BackupService(database_url=migrated_database_url, backend=backend)
    restored = temporary_test_directory / "restore" / "assistant.db"

    snapshot_id = service.restore(snapshot="latest", target=restored)

    assert snapshot_id == "abc12345"
    validate_sqlite(restored)
    with pytest.raises(BackupError, match="已存在"):
        service.restore(snapshot="latest", target=restored)
    with pytest.raises(BackupError, match="正式資料庫"):
        service.restore(snapshot="latest", target=live_database)


def test_next_daily_run_uses_utc_and_rolls_to_the_next_day() -> None:
    before = datetime(2026, 8, 4, 18, 59, tzinfo=UTC)
    after = datetime(2026, 8, 4, 19, 1, tzinfo=UTC)

    assert next_daily_run(now=before, daily_time=time(19, 0)) == datetime(
        2026, 8, 4, 19, 0, tzinfo=UTC
    )
    assert next_daily_run(now=after, daily_time=time(19, 0)) == datetime(
        2026, 8, 5, 19, 0, tzinfo=UTC
    )


def test_backup_settings_contain_only_password_file_path() -> None:
    settings = BackupSettings(
        _env_file=None,
        restic_password_file="/run/secrets/restic_password",
        backup_daily_time_utc="19:00",
    )

    assert settings.daily_time == time(19, 0)
    assert not hasattr(settings, "restic_password")

    with pytest.raises(ValueError, match="HH:MM"):
        BackupSettings(_env_file=None, backup_daily_time_utc="7:00")
