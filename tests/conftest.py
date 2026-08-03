from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import pytest_asyncio

from app.storage.database import Database, upgrade_database
from app.storage.repositories import MessageRepository


@pytest.fixture
def temporary_test_directory() -> Iterator[Path]:
    """建立由目前 Windows 身分獨立管理的測試暫存目錄。"""
    with TemporaryDirectory(prefix="discord-assistant-tests-") as directory:
        yield Path(directory)


@pytest.fixture
def migrated_database_url(temporary_test_directory: Path) -> str:
    database_path = (temporary_test_directory / "discord-assistant.sqlite3").as_posix()
    database_url = f"sqlite+aiosqlite:///{database_path}"
    upgrade_database(database_url)
    return database_url


@pytest_asyncio.fixture
async def database(migrated_database_url: str) -> AsyncIterator[Database]:
    instance = Database(migrated_database_url)
    yield instance
    await instance.dispose()


@pytest_asyncio.fixture
async def message_repository(database: Database) -> MessageRepository:
    return MessageRepository(database.session_factory)
