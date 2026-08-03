from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

from app.storage.database import Database, upgrade_database
from app.storage.repositories import MessageRepository


@pytest.fixture
def migrated_database_url(tmp_path: Path) -> str:
    database_path = (tmp_path / "discord-assistant.sqlite3").as_posix()
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

