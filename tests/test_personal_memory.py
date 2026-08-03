from datetime import UTC, datetime

import pytest

from app.bot.memory_commands import PersonalMemoryCommandGroup
from app.memory.personal_memory import PersonalMemoryService
from app.security.sensitive_filter import SensitiveFilter
from app.storage.database import Database
from app.storage.personal_memories import PersonalMemoryRepository


def _service(database: Database) -> tuple[PersonalMemoryRepository, PersonalMemoryService]:
    repository = PersonalMemoryRepository(database.session_factory)
    return repository, PersonalMemoryService(
        repository,
        sensitive_filter=SensitiveFilter(),
    )


@pytest.mark.asyncio
async def test_explicit_chat_memory_event_is_free_idempotent_and_user_scoped(
    database: Database,
) -> None:
    repository, service = _service(database)

    first = await service.capture_explicit_message(
        guild_id="1",
        user_id="10",
        message_id="100",
        content="請記得我喜歡肉桂捲",
    )
    replay = await service.capture_explicit_message(
        guild_id="1",
        user_id="10",
        message_id="100",
        content="請記得我喜歡肉桂捲",
    )

    assert first.status == "created"
    assert replay.status == "duplicate"
    assert first.save_result is not None
    assert first.save_result.memory.content == "我喜歡肉桂捲"
    assert len(await repository.list_for_user(guild_id="1", user_id="10")) == 1
    assert await repository.list_for_user(guild_id="1", user_id="11") == ()


@pytest.mark.asyncio
async def test_explicit_memory_event_accepts_leading_bot_mention(database: Database) -> None:
    repository, service = _service(database)

    outcome = await service.capture_explicit_message(
        guild_id="1",
        user_id="10",
        message_id="101",
        content="<@999> 請記得我很喜歡音樂遊戲",
    )

    assert outcome.status == "created"
    memories = await repository.list_for_user(guild_id="1", user_id="10")
    assert memories[0].content == "我很喜歡音樂遊戲"


@pytest.mark.asyncio
async def test_ordinary_chat_is_not_guessed_as_permanent_memory(database: Database) -> None:
    repository, service = _service(database)

    outcome = await service.capture_explicit_message(
        guild_id="1",
        user_id="10",
        message_id="100",
        content="我今天吃了肉桂捲",
    )

    assert outcome.status == "not_memory_event"
    assert await repository.list_for_user(guild_id="1", user_id="10") == ()


@pytest.mark.asyncio
async def test_sensitive_memory_is_rejected_without_database_write(database: Database) -> None:
    repository, service = _service(database)
    secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"

    outcome = await service.capture_explicit_message(
        guild_id="1",
        user_id="10",
        message_id="100",
        content=f"請記得我的金鑰是 {secret}",
    )

    assert outcome.status == "blocked_sensitive"
    assert await repository.list_for_user(guild_id="1", user_id="10") == ()


@pytest.mark.asyncio
async def test_user_can_update_and_delete_only_own_memory(database: Database) -> None:
    repository, service = _service(database)
    saved = await repository.create(
        guild_id="1",
        user_id="10",
        content="我喜歡鮭魚三明治",
        source_type="slash",
        now=datetime(2026, 8, 4, tzinfo=UTC),
    )

    assert await service.update_manual(
        guild_id="1",
        user_id="11",
        memory_id=saved.memory.id,
        content="竄改別人的記憶",
    ) is None
    assert not await service.delete_manual(
        guild_id="1",
        user_id="11",
        memory_id=saved.memory.id,
    )

    updated = await service.update_manual(
        guild_id="1",
        user_id="10",
        memory_id=saved.memory.id,
        content="我最喜歡肉桂捲",
    )
    assert updated is not None
    assert updated.content == "我最喜歡肉桂捲"
    assert await service.delete_manual(
        guild_id="1",
        user_id="10",
        memory_id=saved.memory.id,
    )
    assert await service.list_own(guild_id="1", user_id="10") == ()


def test_slash_group_has_no_target_user_parameter(database: Database) -> None:
    """三個命令都只能從 Interaction 取得目前使用者。"""

    _, service = _service(database)
    group = PersonalMemoryCommandGroup(
        service=service,
        allowed_guild_ids=frozenset({1}),
    )

    commands = {command.name: command for command in group.commands}
    assert set(commands) == {"view", "set", "delete"}
    assert "user" not in {parameter.name for parameter in commands["set"].parameters}
    assert "user" not in {parameter.name for parameter in commands["delete"].parameters}
