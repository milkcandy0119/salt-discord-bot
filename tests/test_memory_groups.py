import pytest

from app.storage.database import Database
from app.storage.memory_groups import ChannelAccessRepository, MemoryGroupError


@pytest.mark.asyncio
async def test_memory_group_scope_is_shared_only_by_group_members(database: Database) -> None:
    repository = ChannelAccessRepository(database.session_factory)
    for channel_id in ("10", "11", "12", "13"):
        assert await repository.add_allowed(guild_id="1", channel_id=channel_id)

    group = await repository.create_group(guild_id="1", name="main", description="共同記憶")
    assert group.channel_ids == ()
    await repository.add_channel(guild_id="1", group_name="main", channel_id="10")
    await repository.add_channel(guild_id="1", group_name="main", channel_id="11")
    await repository.add_channel(guild_id="1", group_name="main", channel_id="12")

    assert await repository.visible_channel_ids(guild_id="1", channel_id="10") == ("10", "11", "12")
    assert await repository.visible_channel_ids(guild_id="1", channel_id="13") == ("13",)

    assert await repository.remove_channel(guild_id="1", group_name="main", channel_id="11")
    assert await repository.visible_channel_ids(guild_id="1", channel_id="11") == ("11",)
    assert await repository.visible_channel_ids(guild_id="1", channel_id="10") == ("10", "12")

    assert await repository.delete_group(guild_id="1", name="main")
    assert await repository.visible_channel_ids(guild_id="1", channel_id="10") == ("10",)
    assert await repository.visible_channel_ids(guild_id="1", channel_id="12") == ("12",)


@pytest.mark.asyncio
async def test_memory_group_requires_allowlist_and_prevents_duplicate_membership(
    database: Database,
) -> None:
    repository = ChannelAccessRepository(database.session_factory)
    await repository.create_group(guild_id="1", name="first")
    await repository.create_group(guild_id="1", name="second")

    with pytest.raises(MemoryGroupError, match="白名單"):
        await repository.add_channel(guild_id="1", group_name="first", channel_id="10")

    await repository.add_allowed(guild_id="1", channel_id="10")
    await repository.add_channel(guild_id="1", group_name="first", channel_id="10")
    with pytest.raises(MemoryGroupError, match="其他記憶分組"):
        await repository.add_channel(guild_id="1", group_name="second", channel_id="10")


@pytest.mark.asyncio
async def test_allowlist_is_persistent_and_removal_keeps_group_memory_configuration(
    database: Database,
) -> None:
    repository = ChannelAccessRepository(database.session_factory)
    assert await repository.add_allowed(guild_id="1", channel_id="10")
    assert not await repository.add_allowed(guild_id="1", channel_id="10")
    assert await repository.list_allowed(guild_id="1") == ("10",)
    await repository.create_group(guild_id="1", name="main")
    await repository.add_channel(guild_id="1", group_name="main", channel_id="10")

    assert await repository.remove_allowed(guild_id="1", channel_id="10")
    assert not await repository.is_allowed(guild_id="1", channel_id="10")
    assert await repository.visible_channel_ids(guild_id="1", channel_id="10") == ("10",)


@pytest.mark.asyncio
async def test_seed_only_migrates_env_channels_when_persistent_allowlist_is_empty(
    database: Database,
) -> None:
    repository = ChannelAccessRepository(database.session_factory)
    await repository.seed_allowlist(guild_ids=frozenset({1}), channel_ids=frozenset({10}))
    assert await repository.list_allowed(guild_id="1") == ("10",)

    assert await repository.remove_allowed(guild_id="1", channel_id="10")
    await repository.add_allowed(guild_id="1", channel_id="11")
    await repository.seed_allowlist(guild_ids=frozenset({1}), channel_ids=frozenset({10}))
    assert await repository.list_allowed(guild_id="1") == ("11",)


@pytest.mark.asyncio
async def test_memory_group_can_be_renamed_and_re_described(database: Database) -> None:
    repository = ChannelAccessRepository(database.session_factory)
    created = await repository.create_group(guild_id="1", name="before", description="old")

    edited = await repository.edit_group(
        guild_id="1", name="before", new_name="after", description="new"
    )

    assert edited is not None
    assert edited.id == created.id
    assert edited.name == "after"
    assert edited.description == "new"
