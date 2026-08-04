"""SQLite 一致性快照與 Restic 加密備份協調。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO, Protocol

from app.health import HealthCheckError, sqlite_path_from_url

_SNAPSHOT_ID = re.compile(r"[0-9a-fA-F]{4,64}")
_BACKUP_FILENAME = "discord_assistant.db"


class BackupError(RuntimeError):
    """備份或還原未完成；訊息不得包含祕密與資料內容。"""


@dataclass(frozen=True, slots=True)
class BackupResult:
    """一筆已加密、還原驗證並標記成功的快照。"""

    snapshot_id: str


class BackupBackend(Protocol):
    """讓備份政策不依賴特定目的地實作。"""

    def initialize(self) -> None: ...

    def backup_file(self, source: Path) -> str: ...

    def verify_snapshot(self, snapshot_id: str) -> None: ...

    def mark_verified(self, snapshot_id: str) -> str: ...

    def retain_verified(self, keep_last: int) -> None: ...

    def restore_snapshot(self, snapshot: str, target: Path) -> str: ...


def validate_sqlite(path: Path) -> None:
    """確認快照可開啟，而且 SQLite 完整性檢查結果為 ok。"""

    if not path.is_file():
        raise BackupError("SQLite 快照不存在")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as error:
        raise BackupError("SQLite 快照無法讀取") from error
    if result is None or result[0] != "ok":
        raise BackupError("SQLite 快照完整性檢查失敗")


def create_sqlite_snapshot(database_url: str, target: Path) -> None:
    """使用 SQLite Online Backup API 複製包含 WAL 已提交內容的一致性快照。"""

    try:
        source = sqlite_path_from_url(database_url)
    except HealthCheckError as error:
        raise BackupError(str(error)) from error
    if not source.is_file():
        raise BackupError("正式 SQLite 資料庫不存在")
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{source.as_uri()}?mode=ro"
    try:
        with (
            closing(sqlite3.connect(source_uri, uri=True, timeout=10)) as source_connection,
            closing(sqlite3.connect(target)) as target_connection,
        ):
            source_connection.backup(target_connection)
    except sqlite3.Error as error:
        target.unlink(missing_ok=True)
        raise BackupError("無法建立 SQLite 一致性快照") from error
    validate_sqlite(target)


class ResticBackend:
    """使用密碼檔與參數陣列呼叫 Restic，避免祕密出現在命令列。"""

    def __init__(
        self,
        *,
        repository: str,
        password_file: str,
        binary: str = "restic",
        timeout_seconds: int = 3_600,
    ) -> None:
        self._repository = repository
        self._password_file = Path(password_file)
        self._binary = binary
        self._timeout_seconds = timeout_seconds

    def _environment(self) -> dict[str, str]:
        if not self._password_file.is_file():
            raise BackupError("找不到 Restic 密碼檔")
        environment = os.environ.copy()
        environment["RESTIC_REPOSITORY"] = self._repository
        environment["RESTIC_PASSWORD_FILE"] = str(self._password_file)
        return environment

    def _run(
        self,
        arguments: list[str],
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | int | None = subprocess.PIPE,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self._binary, *arguments],
                stdin=stdin,
                stdout=stdout,
                stderr=subprocess.PIPE,
                env=self._environment(),
                check=True,
                timeout=self._timeout_seconds,
            )
        except FileNotFoundError as error:
            raise BackupError("找不到 Restic 執行檔") from error
        except subprocess.TimeoutExpired as error:
            raise BackupError("Restic 操作逾時") from error
        except subprocess.CalledProcessError as error:
            raise BackupError(f"Restic 操作失敗 exit_code={error.returncode}") from error

    def initialize(self) -> None:
        """明確初始化空白加密儲存庫，不在每日工作中自動猜測。"""

        self._run(["init"])

    def backup_file(self, source: Path) -> str:
        """由 stdin 保存固定檔名，避免暫存目錄名稱進入快照。"""

        with source.open("rb") as source_file:
            result = self._run(
                [
                    "backup",
                    "--stdin",
                    "--stdin-filename",
                    _BACKUP_FILENAME,
                    "--tag",
                    "discord-assistant",
                    "--tag",
                    "pending",
                    "--json",
                ],
                stdin=source_file,
            )
        for line in reversed(result.stdout.decode("utf-8", errors="replace").splitlines()):
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            snapshot_id = message.get("snapshot_id")
            if isinstance(snapshot_id, str) and _SNAPSHOT_ID.fullmatch(snapshot_id):
                return snapshot_id
        raise BackupError("Restic 未回傳有效快照 ID")

    def _dump(self, snapshot_id: str, target: Path) -> None:
        partial = target.with_name(f".{target.name}.partial")
        partial.unlink(missing_ok=True)
        try:
            with partial.open("xb") as output:
                self._run(
                    ["dump", snapshot_id, f"/{_BACKUP_FILENAME}"],
                    stdout=output,
                )
            validate_sqlite(partial)
            partial.replace(target)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def verify_snapshot(self, snapshot_id: str) -> None:
        """讀取全部儲存庫資料，並實際解密還原本次 SQLite 快照。"""

        self._run(["check", "--read-data"])
        with TemporaryDirectory(prefix="discord-assistant-verify-") as directory:
            self._dump(snapshot_id, Path(directory) / _BACKUP_FILENAME)

    def mark_verified(self, snapshot_id: str) -> str:
        """取得 verified 標記，並回傳 Restic 標記後產生的新快照 ID。"""

        self._run(["tag", "--add", "verified", "--remove", "pending", snapshot_id])
        result = self._run(["snapshots", "--tag", "verified", "--json"])
        try:
            snapshots = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise BackupError("無法確認已驗證快照 ID") from error
        for snapshot in snapshots:
            if snapshot.get("original") == snapshot_id:
                verified_id = snapshot.get("id")
                if isinstance(verified_id, str) and _SNAPSHOT_ID.fullmatch(verified_id):
                    return verified_id
        raise BackupError("找不到標記後的已驗證快照")

    def retain_verified(self, keep_last: int) -> None:
        """只輪替已驗證快照；此方法只能在新快照成功後呼叫。"""

        self._run(
            [
                "forget",
                "--tag",
                "verified",
                "--keep-last",
                str(keep_last),
                "--prune",
            ]
        )

    def _resolve_verified_snapshot(self, snapshot: str) -> str:
        arguments = ["snapshots", "--tag", "verified"]
        if snapshot == "latest":
            arguments.extend(["--latest", "1"])
        elif _SNAPSHOT_ID.fullmatch(snapshot):
            arguments.append(snapshot)
        else:
            raise BackupError("快照 ID 格式不正確")
        result = self._run([*arguments, "--json"])
        try:
            snapshots = json.loads(result.stdout)
            snapshot_id = snapshots[0]["id"]
        except (json.JSONDecodeError, IndexError, KeyError, TypeError) as error:
            raise BackupError("找不到已驗證備份") from error
        if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
            raise BackupError("Restic 回傳的快照 ID 不正確")
        if snapshot != "latest" and not snapshot_id.startswith(snapshot.lower()):
            raise BackupError("指定快照尚未通過驗證")
        return snapshot_id

    def restore_snapshot(self, snapshot: str, target: Path) -> str:
        """只解密指定的已驗證快照到新檔案。"""

        snapshot_id = self._resolve_verified_snapshot(snapshot)
        self._dump(snapshot_id, target)
        return snapshot_id


class BackupService:
    """確保順序固定為快照、加密、驗證、標記，最後才輪替。"""

    def __init__(
        self,
        *,
        database_url: str,
        backend: BackupBackend,
        keep_last: int = 7,
    ) -> None:
        self._database_url = database_url
        self._backend = backend
        self._keep_last = keep_last

    def initialize(self) -> None:
        self._backend.initialize()

    def run_backup(self) -> BackupResult:
        """任何驗證失敗都會在輪替前中止，因此舊備份不會被刪除。"""

        with TemporaryDirectory(prefix="discord-assistant-backup-") as directory:
            snapshot = Path(directory) / _BACKUP_FILENAME
            create_sqlite_snapshot(self._database_url, snapshot)
            snapshot_id = self._backend.backup_file(snapshot)
            self._backend.verify_snapshot(snapshot_id)
            snapshot_id = self._backend.mark_verified(snapshot_id)
            self._backend.retain_verified(self._keep_last)
        return BackupResult(snapshot_id=snapshot_id)

    def verify(self, snapshot: str = "latest") -> None:
        """重新驗證指定快照；latest 由後端限制為已驗證快照。"""

        if snapshot == "latest":
            with TemporaryDirectory(prefix="discord-assistant-restore-check-") as directory:
                target = Path(directory) / _BACKUP_FILENAME
                self.restore(snapshot=snapshot, target=target)
            return
        self._backend.verify_snapshot(snapshot)

    def restore(self, *, snapshot: str, target: Path) -> str:
        """拒絕覆寫任何既有檔案，也拒絕把結果寫到正式資料庫路徑。"""

        try:
            live_database = sqlite_path_from_url(self._database_url)
        except HealthCheckError as error:
            raise BackupError(str(error)) from error
        resolved_target = target.expanduser().resolve()
        if resolved_target == live_database:
            raise BackupError("還原目標不得是正式資料庫")
        if resolved_target.exists():
            raise BackupError("還原目標已存在，為避免覆寫已中止")
        resolved_target.parent.mkdir(parents=True, exist_ok=True)
        try:
            snapshot_id = self._backend.restore_snapshot(snapshot, resolved_target)
            validate_sqlite(resolved_target)
        except Exception:
            resolved_target.unlink(missing_ok=True)
            raise
        return snapshot_id


def next_daily_run(*, now: datetime, daily_time: time) -> datetime:
    """計算下一個 UTC 執行時間，當日時間已過便排到隔日。"""

    if now.tzinfo is None:
        raise ValueError("now 必須包含時區")
    utc_now = now.astimezone(UTC)
    candidate = datetime.combine(utc_now.date(), daily_time, tzinfo=UTC)
    return candidate if candidate > utc_now else candidate + timedelta(days=1)
