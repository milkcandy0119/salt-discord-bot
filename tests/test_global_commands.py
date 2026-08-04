from dataclasses import dataclass

import discord
import pytest

from app.ai.persona import Persona
from app.bot.client import DiscordAssistantClient
from app.bot.global_commands import SaltGlobalCommandGroup
from app.config import Settings


@dataclass
class FakeClientStatus:
    latency: float = 0.123

    @staticmethod
    def is_ready() -> bool:
        return True


class FakeInteractionResponse:
    def __init__(self) -> None:
        self.content: str | None = None
        self.ephemeral: bool | None = None
        self.allowed_mentions: object | None = None

    async def send_message(self, content: str, **kwargs: object) -> None:
        self.content = content
        self.ephemeral = bool(kwargs.get("ephemeral"))
        self.allowed_mentions = kwargs.get("allowed_mentions")


class FakeInteraction:
    def __init__(self, guild_id: int) -> None:
        self.guild_id = guild_id
        self.response = FakeInteractionResponse()


PERSONA = Persona(
    identifier="salt-zh-tw",
    version="v1.4",
    display_name="Salt／ソルト",
    instructions="測試人設",
)


def make_group() -> SaltGlobalCommandGroup:
    return SaltGlobalCommandGroup(
        client=FakeClientStatus(),
        persona=PERSONA,
        allowed_guild_ids=frozenset({1}),
    )


async def invoke(
    group: SaltGlobalCommandGroup,
    command_name: str,
    interaction: FakeInteraction,
) -> None:
    command = next(command for command in group.commands if command.name == command_name)
    await command.callback(group, interaction)  # type: ignore[misc, arg-type]


@pytest.mark.asyncio
async def test_global_salt_commands_are_free_private_and_guild_only() -> None:
    group = make_group()

    assert group.guild_only is True
    assert group.allowed_contexts is not None
    assert group.allowed_contexts.guild is True
    assert group.allowed_contexts.dm_channel is False
    assert group.allowed_contexts.private_channel is False
    assert group.allowed_installs is not None
    assert group.allowed_installs.guild is True
    assert group.allowed_installs.user is False
    assert {command.name for command in group.commands} == {
        "about",
        "help",
        "privacy",
        "ping",
    }
    for command_name in ("about", "help", "privacy", "ping"):
        interaction = FakeInteraction(guild_id=1)
        await invoke(group, command_name, interaction)
        assert interaction.response.content
        assert interaction.response.ephemeral is True
        assert isinstance(interaction.response.allowed_mentions, discord.AllowedMentions)


@pytest.mark.asyncio
async def test_global_help_distinguishes_enabled_and_unconfigured_guilds() -> None:
    group = make_group()
    enabled = FakeInteraction(guild_id=1)
    unavailable = FakeInteraction(guild_id=2)

    await invoke(group, "help", enabled)
    await invoke(group, "help", unavailable)

    assert enabled.response.content is not None
    assert "已啟用完整功能" in enabled.response.content
    assert unavailable.response.content is not None
    assert "聊天與個人功能尚未啟用" in unavailable.response.content


@pytest.mark.asyncio
async def test_command_tree_never_places_private_groups_in_global_scope() -> None:
    settings = Settings(
        _env_file=None,
        discord_allowed_guild_ids="1",
        discord_allowed_channel_ids="2",
        discord_owner_user_id="9",
    )
    placeholder = object()
    client = DiscordAssistantClient(
        settings=settings,
        repository=placeholder,  # type: ignore[arg-type]
        segmenter=placeholder,  # type: ignore[arg-type]
        budget_manager=placeholder,  # type: ignore[arg-type]
        context_builder=placeholder,  # type: ignore[arg-type]
        chat_service=placeholder,  # type: ignore[arg-type]
        persona=PERSONA,
        background_repository=placeholder,  # type: ignore[arg-type]
        personal_memory_service=placeholder,  # type: ignore[arg-type]
        reminder_service=placeholder,  # type: ignore[arg-type]
        reminder_repository=placeholder,  # type: ignore[arg-type]
        admin_audit_repository=placeholder,  # type: ignore[arg-type]
        trial_repository=placeholder,  # type: ignore[arg-type]
    )
    try:
        global_names = {command.name for command in client.tree.get_commands()}
        guild_names = {
            command.name
            for command in client.tree.get_commands(guild=discord.Object(id=1))
        }
        sync_scopes: list[int | None] = []

        async def fake_sync(*, guild: discord.Object | None = None) -> list[object]:
            sync_scopes.append(guild.id if guild is not None else None)
            return []

        client.tree.sync = fake_sync  # type: ignore[method-assign]
        await client._sync_application_commands()  # noqa: SLF001
    finally:
        await client.close()

    assert global_names == {"salt"}
    assert guild_names == {"bot", "memory", "remind", "timezone", "trial"}
    assert sync_scopes == [None, 1]
