"""非同步資料庫連線與 migration 入口。"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class Database:
    """提供每個非同步工作各自使用的資料庫 session。"""

    def __init__(self, database_url: str) -> None:
        self.engine = create_async_engine(database_url, pool_pre_ping=True)
        if self.engine.url.get_backend_name() == "sqlite":
            self._enable_sqlite_foreign_keys()
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    def _enable_sqlite_foreign_keys(self) -> None:
        """讓 SQLite 實際執行 schema 中宣告的外鍵規則。"""

        @event.listens_for(self.engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection: object, connection_record: object) -> None:
            del connection_record
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    async def dispose(self) -> None:
        """關閉資料庫連線池。"""

        await self.engine.dispose()


def _ensure_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return
    Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def upgrade_database(database_url: str, *, revision: str = "head") -> None:
    """將資料庫升級至指定 migration；應在事件迴圈外執行。"""

    _ensure_sqlite_parent(database_url)
    project_root = Path(__file__).resolve().parents[2]
    config = Config(project_root / "alembic.ini")
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, revision)
