"""管理員限定的持久化頻道白名單與記憶分組 Slash Commands。"""

from __future__ import annotations

import discord
from discord import app_commands

from app.storage.admin_audit import AdminAuditRepository
from app.storage.memory_groups import ChannelAccessRepository, MemoryGroupError


class AdminMemoryCommandGroup(app_commands.Group):
    """提供 /admin allowlist 與 /admin memory-group，所有結果皆私密。"""

    def __init__(
        self,
        *,
        repository: ChannelAccessRepository,
        audit_repository: AdminAuditRepository,
        allowed_guild_ids: frozenset[int],
        admin_user_ids: frozenset[int],
    ) -> None:
        super().__init__(name="admin", description="管理頻道白名單與記憶分組")
        self._repository = repository
        self._audit_repository = audit_repository
        self._allowed_guild_ids = allowed_guild_ids
        self._admin_user_ids = admin_user_ids
        self.add_command(_AllowlistGroup(parent=self))
        self.add_command(_MemoryGroupGroup(parent=self))

    async def authorize(self, interaction: discord.Interaction) -> tuple[str, str] | None:
        if (
            interaction.guild_id is None
            or interaction.guild_id not in self._allowed_guild_ids
            or interaction.user.id not in self._admin_user_ids
        ):
            await self.respond(interaction, "你沒有管理頻道白名單或記憶分組的權限")
            return None
        return str(interaction.guild_id), str(interaction.user.id)

    @staticmethod
    async def respond(interaction: discord.Interaction, content: str) -> None:
        await interaction.response.send_message(
            content, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )

    @staticmethod
    def validate_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> bool:
        return interaction.guild_id is not None and channel.guild.id == interaction.guild_id


class _AllowlistGroup(app_commands.Group):
    def __init__(self, *, parent: AdminMemoryCommandGroup) -> None:
        super().__init__(name="allowlist", description="管理可讀取與回覆的頻道")
        self._parent = parent

    @app_commands.command(name="list", description="私密列出目前伺服器的允許頻道")
    async def list_channels(self, interaction: discord.Interaction) -> None:
        identity = await self._parent.authorize(interaction)
        if identity is None:
            return
        guild_id, actor_id = identity
        channels = await self._parent._repository.list_allowed(guild_id=guild_id)
        await self._parent._audit_repository.record(
            guild_id=guild_id, actor_user_id=actor_id, action="allowlist_list"
        )
        await self._parent.respond(
            interaction,
            "目前沒有允許頻道"
            if not channels
            else "允許頻道：\n" + "\n".join(f"<#{id}>" for id in channels),
        )

    @app_commands.command(name="add", description="將文字頻道加入白名單")
    async def add(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        identity = await self._parent.authorize(interaction)
        if identity is None:
            return
        if not self._parent.validate_channel(interaction, channel):
            await self._parent.respond(interaction, "只能管理目前伺服器的頻道")
            return
        guild_id, actor_id = identity
        created = await self._parent._repository.add_allowed(
            guild_id=guild_id, channel_id=str(channel.id)
        )
        await self._parent._audit_repository.record(
            guild_id=guild_id, actor_user_id=actor_id, action="allowlist_add"
        )
        await self._parent.respond(interaction, "已加入白名單" if created else "此頻道已在白名單中")

    @app_commands.command(name="remove", description="從白名單移除文字頻道")
    async def remove(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        identity = await self._parent.authorize(interaction)
        if identity is None:
            return
        if not self._parent.validate_channel(interaction, channel):
            await self._parent.respond(interaction, "只能管理目前伺服器的頻道")
            return
        guild_id, actor_id = identity
        removed = await self._parent._repository.remove_allowed(
            guild_id=guild_id, channel_id=str(channel.id)
        )
        await self._parent._audit_repository.record(
            guild_id=guild_id, actor_user_id=actor_id, action="allowlist_remove"
        )
        await self._parent.respond(
            interaction, "已從白名單移除" if removed else "此頻道不在白名單中"
        )

    @app_commands.command(name="sync", description="說明安全的頻道歷史同步流程")
    async def sync(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        """歷史同步可能產生 AI 費用，保留給已估價且確認的 CLI 流程。"""
        identity = await self._parent.authorize(interaction)
        if identity is None:
            return
        if not self._parent.validate_channel(interaction, channel):
            await self._parent.respond(interaction, "只能管理目前伺服器的頻道")
            return
        guild_id, actor_id = identity
        if not await self._parent._repository.is_allowed(
            guild_id=guild_id, channel_id=str(channel.id)
        ):
            await self._parent.respond(interaction, "請先將頻道加入白名單，再進行歷史同步")
            return
        await self._parent._audit_repository.record(
            guild_id=guild_id, actor_user_id=actor_id, action="allowlist_sync_requested"
        )
        await self._parent.respond(
            interaction,
            "歷史同步需先以 CLI 執行免費分析並明確批准費用；"
            "此 Slash Command 不會讀取歷史或呼叫 AI。",
        )


class _MemoryGroupGroup(app_commands.Group):
    def __init__(self, *, parent: AdminMemoryCommandGroup) -> None:
        super().__init__(name="memory-group", description="管理跨頻道歷史記憶範圍")
        self._parent = parent

    @app_commands.command(name="list", description="私密列出記憶分組與其頻道")
    async def list_groups(self, interaction: discord.Interaction) -> None:
        identity = await self._parent.authorize(interaction)
        if identity is None:
            return
        guild_id, actor_id = identity
        groups = await self._parent._repository.list_groups(guild_id=guild_id)
        await self._parent._audit_repository.record(
            guild_id=guild_id, actor_user_id=actor_id, action="memory_group_list"
        )
        lines = [
            f"{group.name}：{group.description or '（無說明）'}；頻道："
            + (", ".join(f"<#{id}>" for id in group.channel_ids) or "（無）")
            for group in groups
        ]
        await self._parent.respond(
            interaction, "目前沒有記憶分組" if not lines else "\n".join(lines)
        )

    @app_commands.command(name="create", description="建立跨頻道記憶分組")
    async def create(
        self, interaction: discord.Interaction, name: str, description: str = ""
    ) -> None:
        identity = await self._parent.authorize(interaction)
        if identity is None:
            return
        guild_id, actor_id = identity
        try:
            group = await self._parent._repository.create_group(
                guild_id=guild_id, name=name, description=description
            )
        except MemoryGroupError as error:
            await self._parent.respond(interaction, str(error))
            return
        await self._parent._audit_repository.record(
            guild_id=guild_id,
            actor_user_id=actor_id,
            action="memory_group_create",
            target_record_id=group.id,
        )
        await self._parent.respond(interaction, f"已建立記憶分組「{group.name}」")

    @app_commands.command(name="delete", description="刪除記憶分組（不刪除訊息或摘要）")
    async def delete(self, interaction: discord.Interaction, name: str) -> None:
        identity = await self._parent.authorize(interaction)
        if identity is None:
            return
        guild_id, actor_id = identity
        deleted = await self._parent._repository.delete_group(guild_id=guild_id, name=name)
        await self._parent._audit_repository.record(
            guild_id=guild_id, actor_user_id=actor_id, action="memory_group_delete"
        )
        await self._parent.respond(
            interaction, "已刪除記憶分組" if deleted else "找不到這個記憶分組"
        )

    @app_commands.command(name="edit", description="修改記憶分組名稱或說明")
    async def edit(
        self,
        interaction: discord.Interaction,
        name: str,
        new_name: str | None = None,
        description: str | None = None,
    ) -> None:
        identity = await self._parent.authorize(interaction)
        if identity is None:
            return
        if new_name is None and description is None:
            await self._parent.respond(interaction, "至少提供新的名稱或說明")
            return
        guild_id, actor_id = identity
        try:
            group = await self._parent._repository.edit_group(
                guild_id=guild_id, name=name, new_name=new_name, description=description
            )
        except MemoryGroupError as error:
            await self._parent.respond(interaction, str(error))
            return
        if group is None:
            await self._parent.respond(interaction, "找不到這個記憶分組")
            return
        await self._parent._audit_repository.record(
            guild_id=guild_id,
            actor_user_id=actor_id,
            action="memory_group_edit",
            target_record_id=group.id,
        )
        await self._parent.respond(interaction, f"已更新記憶分組「{group.name}」")

    @app_commands.command(name="add-channel", description="將白名單頻道加入記憶分組")
    async def add_channel(
        self, interaction: discord.Interaction, name: str, channel: discord.TextChannel
    ) -> None:
        await self._change_channel(interaction, name, channel, add=True)

    @app_commands.command(name="remove-channel", description="從記憶分組移除頻道")
    async def remove_channel(
        self, interaction: discord.Interaction, name: str, channel: discord.TextChannel
    ) -> None:
        await self._change_channel(interaction, name, channel, add=False)

    async def _change_channel(
        self,
        interaction: discord.Interaction,
        name: str,
        channel: discord.TextChannel,
        *,
        add: bool,
    ) -> None:
        identity = await self._parent.authorize(interaction)
        if identity is None:
            return
        if not self._parent.validate_channel(interaction, channel):
            await self._parent.respond(interaction, "只能管理目前伺服器的頻道")
            return
        guild_id, actor_id = identity
        try:
            if add:
                await self._parent._repository.add_channel(
                    guild_id=guild_id, group_name=name, channel_id=str(channel.id)
                )
                message, action = "已將頻道加入記憶分組", "memory_group_add_channel"
            else:
                removed = await self._parent._repository.remove_channel(
                    guild_id=guild_id, group_name=name, channel_id=str(channel.id)
                )
                message, action = (
                    ("已將頻道移出記憶分組" if removed else "找不到該分組中的頻道"),
                    "memory_group_remove_channel",
                )
        except MemoryGroupError as error:
            await self._parent.respond(interaction, str(error))
            return
        await self._parent._audit_repository.record(
            guild_id=guild_id, actor_user_id=actor_id, action=action
        )
        await self._parent.respond(interaction, message)
