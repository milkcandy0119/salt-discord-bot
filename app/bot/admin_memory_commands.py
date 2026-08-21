"""管理員限定的持久化頻道白名單與記憶分組 Slash Commands。"""

from __future__ import annotations

import discord
from discord import app_commands

from app.storage.admin_audit import AdminAuditRepository
from app.storage.memory_groups import ChannelAccessRepository, ChannelModeError, MemoryGroupError


class AdminMemoryCommandGroup(app_commands.Group):
    """提供 /admin menu，所有結果皆私密。"""

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

    @app_commands.command(name="menu", description="以選單管理頻道與記憶群組")
    async def menu(self, interaction: discord.Interaction) -> None:
        identity = await self.authorize(interaction)
        if identity is None:
            return
        guild_id, user_id = identity
        await interaction.response.send_message(
            "請選擇管理操作：",
            ephemeral=True,
            view=_AdminMenuView(parent=self, guild_id=guild_id, user_id=user_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )

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


class _AdminMenuView(discord.ui.View):
    def __init__(self, *, parent: AdminMemoryCommandGroup, guild_id: str, user_id: str) -> None:
        super().__init__(timeout=600)
        self.parent = parent
        self.guild_id = guild_id
        self.user_id = user_id
        self.add_item(_AdminActionSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild_id == int(self.guild_id)
            and str(interaction.user.id) == self.user_id
            and interaction.user.id in self.parent._admin_user_ids
        ):
            return True
        await _admin_component_respond(interaction, "這不是你的管理選單")
        return False


class _AdminActionSelect(discord.ui.Select[_AdminMenuView]):
    def __init__(self) -> None:
        super().__init__(
            placeholder="選擇管理操作…",
            options=(
                discord.SelectOption(label="查看白名單頻道", value="allowlist-list"),
                discord.SelectOption(label="新增白名單頻道", value="allowlist-add"),
                discord.SelectOption(label="移除白名單頻道", value="allowlist-remove"),
                discord.SelectOption(label="切換頻道模式", value="channel-mode"),
                discord.SelectOption(label="查看記憶群組", value="group-list"),
                discord.SelectOption(label="建立記憶群組", value="group-create"),
                discord.SelectOption(label="加入頻道到記憶群組", value="group-add-channel"),
                discord.SelectOption(label="移除記憶群組的頻道", value="group-remove-channel"),
                discord.SelectOption(label="刪除記憶群組", value="group-delete"),
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _AdminMenuView):
            await _admin_component_respond(interaction, "管理選單已失效，請重新開啟")
            return
        action = self.values[0]
        try:
            if action == "allowlist-list":
                channels = await view.parent._repository.list_allowed(guild_id=view.guild_id)
                await view.parent._audit_repository.record(
                    guild_id=view.guild_id,
                    actor_user_id=view.user_id,
                    action="allowlist_list",
                )
                await interaction.response.edit_message(
                    content="目前沒有白名單頻道"
                    if not channels
                    else "\n".join(f"<#{channel}>" for channel in channels),
                    view=None,
                )
                return
            if action == "allowlist-add":
                await interaction.response.edit_message(
                    content="選擇要加入白名單的文字頻道：",
                    view=_AllowlistChannelView(
                        parent=view.parent, guild_id=view.guild_id, user_id=view.user_id, add=True
                    ),
                )
                return
            if action == "allowlist-remove":
                channels = await view.parent._repository.list_allowed(guild_id=view.guild_id)
                if not channels:
                    await interaction.response.edit_message(content="目前沒有白名單頻道", view=None)
                    return
                await interaction.response.edit_message(
                    content="選擇要移除的頻道：",
                    view=_AllowlistRemoveView(
                        parent=view.parent,
                        guild_id=view.guild_id,
                        user_id=view.user_id,
                        channels=channels[:25],
                    ),
                )
                return
            if action == "channel-mode":
                channels = await view.parent._repository.list_allowed(guild_id=view.guild_id)
                if not channels:
                    await interaction.response.edit_message(content="目前沒有白名單頻道", view=None)
                    return
                await interaction.response.edit_message(
                    content="選擇要切換模式的頻道：",
                    view=_ChannelModePickView(
                        parent=view.parent,
                        guild_id=view.guild_id,
                        user_id=view.user_id,
                        channels=channels[:25],
                    ),
                )
                return
            if action == "group-list":
                groups = await view.parent._repository.list_groups(guild_id=view.guild_id)
                await view.parent._audit_repository.record(
                    guild_id=view.guild_id,
                    actor_user_id=view.user_id,
                    action="memory_group_list",
                )
                content = (
                    "目前沒有記憶群組"
                    if not groups
                    else "\n".join(
                        f"{group.name}：{group.description or '無說明'}" for group in groups
                    )
                )
                await interaction.response.edit_message(content=content, view=None)
                return
            if action == "group-create":
                await interaction.response.send_modal(
                    _MemoryGroupModal(
                        parent=view.parent, guild_id=view.guild_id, user_id=view.user_id
                    )
                )
                return
            groups = await view.parent._repository.list_groups(guild_id=view.guild_id)
        except Exception:
            await _admin_component_respond(interaction, "讀取管理資料失敗，請稍後再試")
            return
        if action in {"group-add-channel", "group-remove-channel"}:
            if not groups:
                await interaction.response.edit_message(
                    content="目前沒有可編輯的記憶群組",
                    view=None,
                )
                return
            await interaction.response.edit_message(
                content="選擇要編輯的記憶群組：",
                view=_MemoryGroupEditPickView(
                    parent=view.parent,
                    guild_id=view.guild_id,
                    user_id=view.user_id,
                    groups=groups[:25],
                    add_channel=action == "group-add-channel",
                ),
            )
            return
        if not groups:
            await interaction.response.edit_message(content="目前沒有可刪除的記憶群組", view=None)
            return
        await interaction.response.edit_message(
            content="選擇要刪除的記憶群組：",
            view=_MemoryGroupDeleteView(
                parent=view.parent, guild_id=view.guild_id, user_id=view.user_id, groups=groups[:25]
            ),
        )


class _AllowlistChannelView(_AdminMenuView):
    def __init__(
        self, *, parent: AdminMemoryCommandGroup, guild_id: str, user_id: str, add: bool
    ) -> None:
        discord.ui.View.__init__(self, timeout=600)
        self.parent = parent
        self.guild_id = guild_id
        self.user_id = user_id
        self.add_item(_AllowlistChannelSelect(add=add))


class _AllowlistChannelSelect(discord.ui.ChannelSelect[_AllowlistChannelView]):
    def __init__(self, *, add: bool) -> None:
        self._add = add
        super().__init__(
            placeholder="選擇文字頻道…",
            channel_types=(discord.ChannelType.text,),
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _AllowlistChannelView):
            await _admin_component_respond(interaction, "管理選單已失效，請重新開啟")
            return
        channel_id = str(self.values[0].id)
        try:
            if self._add:
                changed = await view.parent._repository.add_allowed(
                    guild_id=view.guild_id,
                    channel_id=channel_id,
                )
                action = "allowlist_add"
                message = "已加入白名單" if changed else "該頻道已在白名單中"
            else:
                changed = await view.parent._repository.remove_allowed(
                    guild_id=view.guild_id,
                    channel_id=channel_id,
                )
                action = "allowlist_remove"
                message = "已移出白名單" if changed else "該頻道不在白名單中"
            await view.parent._audit_repository.record(
                guild_id=view.guild_id,
                actor_user_id=view.user_id,
                action=action,
            )
        except Exception:
            await _admin_component_respond(interaction, "更新白名單失敗，請稍後再試")
            return
        await interaction.response.edit_message(content=message, view=None)


class _AllowlistRemoveView(_AllowlistChannelView):
    def __init__(
        self,
        *,
        parent: AdminMemoryCommandGroup,
        guild_id: str,
        user_id: str,
        channels: tuple[str, ...],
    ) -> None:
        discord.ui.View.__init__(self, timeout=600)
        self.parent = parent
        self.guild_id = guild_id
        self.user_id = user_id
        self.add_item(
            _AllowedChannelPickSelect(
                options=tuple(
                    discord.SelectOption(label=f"#{channel}", value=channel) for channel in channels
                )
            )
        )


class _AllowedChannelPickSelect(discord.ui.Select[_AllowlistRemoveView]):
    def __init__(self, *, options: tuple[discord.SelectOption, ...]) -> None:
        super().__init__(placeholder="選擇要移除的頻道…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _AllowlistRemoveView):
            await _admin_component_respond(interaction, "管理選單已失效，請重新開啟")
            return
        channel_id = self.values[0]
        try:
            removed = await view.parent._repository.remove_allowed(
                guild_id=view.guild_id,
                channel_id=channel_id,
            )
            await view.parent._audit_repository.record(
                guild_id=view.guild_id,
                actor_user_id=view.user_id,
                action="allowlist_remove",
            )
        except Exception:
            await _admin_component_respond(interaction, "移除白名單失敗，請稍後再試")
            return
        await interaction.response.edit_message(
            content="已移出白名單" if removed else "該頻道不在白名單中",
            view=None,
        )


class _ChannelModePickView(_AdminMenuView):
    def __init__(
        self,
        *,
        parent: AdminMemoryCommandGroup,
        guild_id: str,
        user_id: str,
        channels: tuple[str, ...],
    ) -> None:
        discord.ui.View.__init__(self, timeout=600)
        self.parent = parent
        self.guild_id = guild_id
        self.user_id = user_id
        self.add_item(
            _ChannelModePickSelect(
                options=tuple(
                    discord.SelectOption(label=f"#{channel_id}", value=channel_id)
                    for channel_id in channels
                )
            )
        )


class _ChannelModePickSelect(discord.ui.Select[_ChannelModePickView]):
    def __init__(self, *, options: tuple[discord.SelectOption, ...]) -> None:
        super().__init__(placeholder="選擇頻道…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _ChannelModePickView):
            await _admin_component_respond(interaction, "管理選單已失效，請重新開啟")
            return
        await interaction.response.edit_message(
            content=f"選擇 #{self.values[0]} 的模式：",
            view=_ChannelModeSelectView(
                parent=view.parent,
                guild_id=view.guild_id,
                user_id=view.user_id,
                channel_id=self.values[0],
            ),
        )


class _ChannelModeSelectView(_AdminMenuView):
    def __init__(
        self,
        *,
        parent: AdminMemoryCommandGroup,
        guild_id: str,
        user_id: str,
        channel_id: str,
    ) -> None:
        discord.ui.View.__init__(self, timeout=600)
        self.parent = parent
        self.guild_id = guild_id
        self.user_id = user_id
        self.channel_id = channel_id
        self.add_item(_ChannelModeSelect())


class _ChannelModeSelect(discord.ui.Select[_ChannelModeSelectView]):
    def __init__(self) -> None:
        super().__init__(
            placeholder="選擇模式…",
            options=(
                discord.SelectOption(
                    label="一般模式",
                    value="normal",
                    description="僅在提及、回覆或 !ai 時回應",
                ),
                discord.SelectOption(
                    label="陪伴模式",
                    value="companion",
                    description="依保守規則可主動加入對話",
                ),
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _ChannelModeSelectView):
            await _admin_component_respond(interaction, "管理選單已失效，請重新開啟")
            return
        mode = self.values[0]
        try:
            await view.parent._repository.set_channel_mode(
                guild_id=view.guild_id,
                channel_id=view.channel_id,
                mode=mode,
            )
            await view.parent._audit_repository.record(
                guild_id=view.guild_id,
                actor_user_id=view.user_id,
                action="channel_mode_update",
                target_record_id=view.channel_id,
            )
        except ChannelModeError as error:
            await _admin_component_respond(interaction, str(error))
            return
        except Exception:
            await _admin_component_respond(interaction, "切換頻道模式失敗，請稍後再試")
            return
        label = "一般模式" if mode == "normal" else "陪伴模式"
        await interaction.response.edit_message(content=f"已切換為{label}", view=None)


class _MemoryGroupModal(discord.ui.Modal, title="建立記憶群組"):
    name = discord.ui.TextInput(label="群組名稱", max_length=100)
    description = discord.ui.TextInput(label="說明（可留白）", max_length=500, required=False)

    def __init__(self, *, parent: AdminMemoryCommandGroup, guild_id: str, user_id: str) -> None:
        super().__init__()
        self._parent = parent
        self._guild_id = guild_id
        self._user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            group = await self._parent._repository.create_group(
                guild_id=self._guild_id,
                name=self.name.value,
                description=self.description.value,
            )
            await self._parent._audit_repository.record(
                guild_id=self._guild_id,
                actor_user_id=self._user_id,
                action="memory_group_create",
                target_record_id=group.id,
            )
        except MemoryGroupError as error:
            await _admin_component_respond(interaction, str(error))
            return
        except Exception:
            await _admin_component_respond(interaction, "建立記憶群組失敗，請稍後再試")
            return
        await _admin_component_respond(interaction, f"已建立記憶群組：{group.name}")


class _MemoryGroupEditPickView(_AdminMenuView):
    def __init__(
        self,
        *,
        parent: AdminMemoryCommandGroup,
        guild_id: str,
        user_id: str,
        groups: tuple[object, ...],
        add_channel: bool,
    ) -> None:
        discord.ui.View.__init__(self, timeout=600)
        self.parent = parent
        self.guild_id = guild_id
        self.user_id = user_id
        self.add_item(_MemoryGroupEditPickSelect(groups=groups, add_channel=add_channel))


class _MemoryGroupEditPickSelect(discord.ui.Select[_MemoryGroupEditPickView]):
    def __init__(self, *, groups: tuple[object, ...], add_channel: bool) -> None:
        self._add_channel = add_channel
        super().__init__(
            placeholder="選擇記憶群組…",
            options=tuple(
                discord.SelectOption(label=group.name[:100], value=group.name) for group in groups
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _MemoryGroupEditPickView):
            await _admin_component_respond(interaction, "管理選單已失效，請重新開啟")
            return
        group_name = self.values[0]
        if self._add_channel:
            await interaction.response.edit_message(
                content=f"選擇要加入「{group_name}」的白名單文字頻道：",
                view=_MemoryGroupAddChannelView(
                    parent=view.parent,
                    guild_id=view.guild_id,
                    user_id=view.user_id,
                    group_name=group_name,
                ),
            )
            return
        try:
            groups = await view.parent._repository.list_groups(guild_id=view.guild_id)
            group = next(item for item in groups if item.name == group_name)
        except Exception:
            await _admin_component_respond(interaction, "讀取記憶群組失敗，請重新開啟選單")
            return
        if not group.channel_ids:
            await interaction.response.edit_message(
                content=f"「{group.name}」目前沒有已加入的頻道",
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=f"選擇要從「{group.name}」移除的頻道：",
            view=_MemoryGroupRemoveChannelView(
                parent=view.parent,
                guild_id=view.guild_id,
                user_id=view.user_id,
                group_name=group.name,
                channel_ids=group.channel_ids[:25],
            ),
        )


class _MemoryGroupAddChannelView(_AdminMenuView):
    def __init__(
        self,
        *,
        parent: AdminMemoryCommandGroup,
        guild_id: str,
        user_id: str,
        group_name: str,
    ) -> None:
        discord.ui.View.__init__(self, timeout=600)
        self.parent = parent
        self.guild_id = guild_id
        self.user_id = user_id
        self.group_name = group_name
        self.add_item(_MemoryGroupAddChannelSelect())


class _MemoryGroupAddChannelSelect(discord.ui.ChannelSelect[_MemoryGroupAddChannelView]):
    def __init__(self) -> None:
        super().__init__(
            placeholder="選擇文字頻道…",
            channel_types=(discord.ChannelType.text,),
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _MemoryGroupAddChannelView):
            await _admin_component_respond(interaction, "管理選單已失效，請重新開啟")
            return
        try:
            await view.parent._repository.add_channel(
                guild_id=view.guild_id,
                group_name=view.group_name,
                channel_id=str(self.values[0].id),
            )
            await view.parent._audit_repository.record(
                guild_id=view.guild_id,
                actor_user_id=view.user_id,
                action="memory_group_add_channel",
            )
        except MemoryGroupError as error:
            await _admin_component_respond(interaction, str(error))
            return
        except Exception:
            await _admin_component_respond(interaction, "更新記憶群組失敗，請稍後再試")
            return
        await interaction.response.edit_message(content="已將頻道加入記憶群組", view=None)


class _MemoryGroupRemoveChannelView(_AdminMenuView):
    def __init__(
        self,
        *,
        parent: AdminMemoryCommandGroup,
        guild_id: str,
        user_id: str,
        group_name: str,
        channel_ids: tuple[str, ...],
    ) -> None:
        discord.ui.View.__init__(self, timeout=600)
        self.parent = parent
        self.guild_id = guild_id
        self.user_id = user_id
        self.group_name = group_name
        self.add_item(
            _MemoryGroupRemoveChannelSelect(
                options=tuple(
                    discord.SelectOption(label=f"#{channel_id}", value=channel_id)
                    for channel_id in channel_ids
                )
            )
        )


class _MemoryGroupRemoveChannelSelect(discord.ui.Select[_MemoryGroupRemoveChannelView]):
    def __init__(self, *, options: tuple[discord.SelectOption, ...]) -> None:
        super().__init__(placeholder="選擇要移除的頻道…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _MemoryGroupRemoveChannelView):
            await _admin_component_respond(interaction, "管理選單已失效，請重新開啟")
            return
        try:
            removed = await view.parent._repository.remove_channel(
                guild_id=view.guild_id,
                group_name=view.group_name,
                channel_id=self.values[0],
            )
            await view.parent._audit_repository.record(
                guild_id=view.guild_id,
                actor_user_id=view.user_id,
                action="memory_group_remove_channel",
            )
        except MemoryGroupError as error:
            await _admin_component_respond(interaction, str(error))
            return
        except Exception:
            await _admin_component_respond(interaction, "更新記憶群組失敗，請稍後再試")
            return
        await interaction.response.edit_message(
            content="已將頻道移出記憶群組" if removed else "找不到該分組中的頻道",
            view=None,
        )


class _MemoryGroupDeleteView(_AdminMenuView):
    def __init__(
        self,
        *,
        parent: AdminMemoryCommandGroup,
        guild_id: str,
        user_id: str,
        groups: tuple[object, ...],
    ) -> None:
        discord.ui.View.__init__(self, timeout=600)
        self.parent = parent
        self.guild_id = guild_id
        self.user_id = user_id
        self.add_item(
            _MemoryGroupPickSelect(
                options=tuple(
                    discord.SelectOption(label=group.name[:100], value=group.name)
                    for group in groups
                )
            )
        )


class _MemoryGroupPickSelect(discord.ui.Select[_MemoryGroupDeleteView]):
    def __init__(self, *, options: tuple[discord.SelectOption, ...]) -> None:
        super().__init__(placeholder="選擇要刪除的群組…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _MemoryGroupDeleteView):
            await _admin_component_respond(interaction, "管理選單已失效，請重新開啟")
            return
        try:
            deleted = await view.parent._repository.delete_group(
                guild_id=view.guild_id,
                name=self.values[0],
            )
            await view.parent._audit_repository.record(
                guild_id=view.guild_id,
                actor_user_id=view.user_id,
                action="memory_group_delete",
            )
        except Exception:
            await _admin_component_respond(interaction, "刪除記憶群組失敗，請稍後再試")
            return
        await interaction.response.edit_message(
            content="已刪除記憶群組" if deleted else "找不到記憶群組",
            view=None,
        )


async def _admin_component_respond(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
        return
    await interaction.response.send_message(content, ephemeral=True)
