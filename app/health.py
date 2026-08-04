"""容器使用的本機心跳與 SQLite 健康檢查。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import make_url


class HealthCheckError(RuntimeError):
    """健康條件不成立，但不包含憑證或資料內容。"""


def sqlite_path_from_url(database_url: str) -> Path:
    """只接受目前部署支援的檔案型 SQLite URL。"""

    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        raise HealthCheckError("健康檢查只支援檔案型 SQLite")
    return Path(url.database).expanduser().resolve()


def write_heartbeat(path: str | Path, *, now: datetime | None = None) -> None:
    """原子更新心跳，避免健康檢查讀到半寫入檔案。"""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    effective_now = now or datetime.now(UTC)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(effective_now.isoformat(), encoding="utf-8")
    temporary.replace(target)


def remove_heartbeat(path: str | Path) -> None:
    """正常停止時移除心跳，避免殘留檔案短暫誤判為健康。"""

    Path(path).unlink(missing_ok=True)


def check_heartbeat(
    path: str | Path,
    *,
    max_age: timedelta,
    now: datetime | None = None,
) -> None:
    """確認心跳存在、格式正確，而且沒有過期或明顯來自未來。"""

    target = Path(path)
    try:
        heartbeat_at = datetime.fromisoformat(target.read_text(encoding="utf-8").strip())
    except (OSError, ValueError) as error:
        raise HealthCheckError("找不到有效的機器人心跳") from error
    if heartbeat_at.tzinfo is None:
        raise HealthCheckError("機器人心跳缺少時區")
    effective_now = now or datetime.now(UTC)
    age = effective_now - heartbeat_at.astimezone(UTC)
    if age < timedelta(seconds=-30):
        raise HealthCheckError("機器人心跳時間晚於系統時間")
    if age > max_age:
        raise HealthCheckError("機器人心跳已過期")


def check_sqlite(database_url: str) -> None:
    """以唯讀模式確認 SQLite 可開啟且目前 schema 可查詢。"""

    database_path = sqlite_path_from_url(database_url)
    if not database_path.is_file():
        raise HealthCheckError("SQLite 資料庫不存在")
    uri = f"{database_path.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=2)) as connection:
            connection.execute("SELECT version_num FROM alembic_version LIMIT 1").fetchone()
    except sqlite3.Error as error:
        raise HealthCheckError("SQLite 無法讀取") from error


def run_health_check(
    *,
    database_url: str,
    heartbeat_path: str | Path,
    max_age_seconds: int,
    now: datetime | None = None,
) -> None:
    """依序驗證事件迴圈心跳與持久化資料庫。"""

    check_heartbeat(
        heartbeat_path,
        max_age=timedelta(seconds=max_age_seconds),
        now=now,
    )
    check_sqlite(database_url)
