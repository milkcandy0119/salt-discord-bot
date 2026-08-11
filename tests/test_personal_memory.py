from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.bot.memory_commands import PersonalMemoryCommandGroup
from app.memory.personal_memory import PersonalMemoryService
from app.security.sensitive_filter import SensitiveFilter
from app.storage.admin_audit import AdminAuditRepository
from app.storage.database import Database
from app.storage.personal_memories import PersonalMemoryRepository


@dataclass
class FakeUser:
    id: int


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.content: str | None = None
        self.ephemeral: bool | None = None

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.content = content
        self.ephemeral = bool(kwargs.get("ephemeral"))


class FakeInteraction:
    def __init__(self, *, guild_id: int, user_id: int) -> None:
        self.guild_id = guild_id
        self.user = FakeUser(user_id)
        self.response = FakeInteractionResponse()


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
async def test_third_party_memory_request_is_not_saved(database: Database) -> None:
    repository, service = _service(database)

    outcome = await service.capture_explicit_message(
        guild_id="1",
        user_id="10",
        message_id="102",
        content="記住黃俊謀是臭企鵝",
    )

    assert outcome.status == "unsupported_memory_subject"
    assert await repository.list_for_user(guild_id="1", user_id="10") == ()


@pytest.mark.asyncio
async def test_ambiguous_forget_request_does_not_delete_memory(database: Database) -> None:
    repository, service = _service(database)
    saved = await repository.create(
        guild_id="1",
        user_id="10",
        content="我喜歡肉桂捲",
        source_type="slash",
        now=datetime(2026, 8, 4, tzinfo=UTC),
    )

    outcome = await service.capture_explicit_message(
        guild_id="1",
        user_id="10",
        message_id="103",
        content="忘記這件事",
    )

    assert outcome.status == "ambiguous_delete"
    assert await repository.list_for_user(guild_id="1", user_id="10") == (
        saved.memory,
    )


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


def test_slash_group_exposes_only_the_menu(database: Database) -> None:
    """三個命令都只能從 Interaction 取得目前使用者。"""

    _, service = _service(database)
    group = PersonalMemoryCommandGroup(
        service=service,
        allowed_guild_ids=frozenset({1}),
    )

    commands = {command.name: command for command in group.commands}
    assert set(commands) == {"menu"}
    assert commands["menu"].parameters == []


@pytest.mark.asyncio
async def _legacy_admin_memory_commands_are_not_registered(
    database: Database,
) -> None:
    repository, service = _service(database)
    audit = AdminAuditRepository(database.session_factory)
    saved = await repository.create(
        guild_id="1",
        user_id="20",
        content="我擅長音樂遊戲",
        source_type="slash",
    )
    group = PersonalMemoryCommandGroup(
        service=service,
        allowed_guild_ids=frozenset({1}),
        admin_user_ids=frozenset({9}),
        audit_repository=audit,
    )
    commands = {command.name: command for command in group.commands}
    target = FakeUser(20)

    denied = FakeInteraction(guild_id=1, user_id=8)
    await commands["admin-view"].callback(  # type: ignore[misc]
        group,
        denied,  # type: ignore[arg-type]
        target,  # type: ignore[arg-type]
    )
    assert denied.response.ephemeral is True
    assert denied.response.content is not None
    assert "沒有" in denied.response.content
    assert await audit.count() == 0

    allowed = FakeInteraction(guild_id=1, user_id=9)
    await commands["admin-view"].callback(  # type: ignore[misc]
        group,
        allowed,  # type: ignore[arg-type]
        target,  # type: ignore[arg-type]
    )
    assert allowed.response.ephemeral is True
    assert allowed.response.content is not None
    assert "我擅長音樂遊戲" in allowed.response.content
    assert await audit.count(action="personal_memory_admin_view") == 1

    modified = FakeInteraction(guild_id=1, user_id=9)
    await commands["admin-set"].callback(  # type: ignore[misc]
        group,
        modified,  # type: ignore[arg-type]
        target,  # type: ignore[arg-type]
        saved.memory.id,
        "我很擅長節奏遊戲",
    )
    target_memories = await repository.list_for_user(guild_id="1", user_id="20")
    assert target_memories[0].content == "我很擅長節奏遊戲"
    assert await audit.count(action="personal_memory_admin_update") == 1
