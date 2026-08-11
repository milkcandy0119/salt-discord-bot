"""只能查看與修改目前 Discord 使用者自己記憶的 Slash Commands。"""

from __future__ import annotations

import discord
from discord import app_commands

from app.memory.personal_memory import (
    InvalidMemoryContentError,
    MemoryConflictError,
    PersonalMemoryService,
    SensitiveMemoryContentError,
)
from app.storage.admin_audit import AdminAuditRepository


class PersonalMemoryCommandGroup(app_commands.Group):
    """將使用者身分固定取自 Interaction，命令不接受目標 user ID。"""

    def __init__(
        self,
        *,
        service: PersonalMemoryService,
        allowed_guild_ids: frozenset[int],
        admin_user_ids: frozenset[int] = frozenset(),
        audit_repository: AdminAuditRepository | None = None,
    ) -> None:
        super().__init__(name="memory", description="管理 Salt 對你自己的基本記憶")
        self._service = service
        self._allowed_guild_ids = allowed_guild_ids
        self._admin_user_ids = admin_user_ids
        self._audit_repository = audit_repository
        # All personal-memory actions are reached through /memory menu.
        # Do not register the legacy parameter-based Slash commands.
        for command_name in ("view", "set", "delete", "admin-view", "admin-set"):
            self.remove_command(command_name)

    @app_commands.command(name="menu", description="以選單管理自己的記憶")
    async def menu(self, interaction: discord.Interaction) -> None:
        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        await interaction.response.send_message(
            "請選擇要進行的記憶操作：",
            ephemeral=True,
            view=_MemoryMenuView(
                service=self._service,
                guild_id=guild_id,
                user_id=user_id,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="view", description="私密查看 Salt 對你自己的記憶")
    async def view(self, interaction: discord.Interaction) -> None:
        """列出目前使用者在此伺服器中的記憶與編號。"""

        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        memories = await self._service.list_own(guild_id=guild_id, user_id=user_id)
        if not memories:
            await self._respond(interaction, "Salt 目前還沒有記住你的個人資料")
            return
        lines = ["Salt 目前對你的記憶："]
        for memory in memories:
            line = f"#{memory.id}　{memory.content}"
            if sum(len(item) + 1 for item in (*lines, line)) > 1_850:
                lines.append("……其餘記憶請先刪除部分內容後再查看")
                break
            lines.append(line)
        await self._respond(interaction, "\n".join(lines))

    @app_commands.command(name="set", description="新增或修改 Salt 對你自己的記憶")
    @app_commands.describe(
        content="想讓 Salt 記住的內容，最多 200 字",
        memory_id="要修改的記憶編號；留空代表新增",
    )
    async def set_memory(
        self,
        interaction: discord.Interaction,
        content: str,
        memory_id: int | None = None,
    ) -> None:
        """新增記憶，或依編號修改目前使用者自己的記憶。"""

        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        try:
            if memory_id is None:
                result = await self._service.create_manual(
                    guild_id=guild_id,
                    user_id=user_id,
                    content=content,
                )
                message = (
                    f"記住了，記憶編號是 #{result.memory.id}"
                    if result.created
                    else f"這個已經記得了，記憶編號是 #{result.memory.id}"
                )
            else:
                memory = await self._service.update_manual(
                    guild_id=guild_id,
                    user_id=user_id,
                    memory_id=memory_id,
                    content=content,
                )
                message = (
                    f"已更新記憶 #{memory.id}" if memory is not None else "找不到屬於你的這筆記憶"
                )
        except (InvalidMemoryContentError, SensitiveMemoryContentError) as error:
            message = str(error)
        except MemoryConflictError:
            message = "你已經有相同內容的記憶"
        await self._respond(interaction, message)

    @app_commands.command(name="delete", description="刪除 Salt 對你自己的指定記憶")
    @app_commands.describe(memory_id="要刪除的記憶編號")
    async def delete_memory(
        self,
        interaction: discord.Interaction,
        memory_id: int,
    ) -> None:
        """依編號刪除目前使用者自己的記憶。"""

        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        deleted = await self._service.delete_manual(
            guild_id=guild_id,
            user_id=user_id,
            memory_id=memory_id,
        )
        await self._respond(
            interaction,
            f"已刪除記憶 #{memory_id}" if deleted else "找不到屬於你的這筆記憶",
        )

    @app_commands.command(
        name="admin-view",
        description="擁有者或指定管理員私密查看成員記憶",
    )
    @app_commands.describe(user="要查看記憶的成員")
    async def admin_view(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ) -> None:
        """經固定 ID 權限檢查後私密列出指定使用者記憶。"""

        admin = await self._admin(interaction)
        if admin is None:
            return
        guild_id, actor_user_id = admin
        memories = await self._service.list_own(
            guild_id=guild_id,
            user_id=str(user.id),
        )
        lines = [f"Salt 對 user_id={user.id} 的記憶："]
        if not memories:
            lines.append("目前沒有記憶")
        for memory in memories:
            line = f"#{memory.id}　{memory.content}"
            if sum(len(item) + 1 for item in (*lines, line)) > 1_850:
                lines.append("……其餘記憶未顯示")
                break
            lines.append(line)
        if self._audit_repository is not None:
            await self._audit_repository.record(
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                action="personal_memory_admin_view",
                target_user_id=str(user.id),
            )
        await self._respond(interaction, "\n".join(lines))

    @app_commands.command(
        name="admin-set",
        description="擁有者或指定管理員修改成員的指定記憶",
    )
    @app_commands.describe(
        user="記憶所屬成員",
        memory_id="要修改的記憶編號",
        content="新的記憶內容，最多 200 字",
    )
    async def admin_set(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        memory_id: int,
        content: str,
    ) -> None:
        """管理員只能修改已存在記憶，不能替成員新增或刪除。"""

        admin = await self._admin(interaction)
        if admin is None:
            return
        guild_id, actor_user_id = admin
        try:
            memory = await self._service.update_manual(
                guild_id=guild_id,
                user_id=str(user.id),
                memory_id=memory_id,
                content=content,
            )
            message = (
                f"已修改 user_id={user.id} 的記憶 #{memory_id}"
                if memory is not None
                else "找不到屬於該使用者的這筆記憶"
            )
        except (InvalidMemoryContentError, SensitiveMemoryContentError) as error:
            memory = None
            message = str(error)
        except MemoryConflictError:
            memory = None
            message = "該使用者已經有相同內容的記憶"
        if memory is not None and self._audit_repository is not None:
            await self._audit_repository.record(
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                action="personal_memory_admin_update",
                target_user_id=str(user.id),
                target_record_id=memory_id,
            )
        await self._respond(interaction, message)

    async def _owner(
        self,
        interaction: discord.Interaction,
    ) -> tuple[str, str] | None:
        """從 Discord Interaction 取得固定擁有者，不接受命令參數代替。"""

        if interaction.guild_id is None or interaction.guild_id not in self._allowed_guild_ids:
            await self._respond(interaction, "這個伺服器不能使用個人記憶功能")
            return None
        return str(interaction.guild_id), str(interaction.user.id)

    async def _admin(
        self,
        interaction: discord.Interaction,
    ) -> tuple[str, str] | None:
        """同時驗證伺服器白名單與固定管理員 Discord user ID。"""

        if (
            interaction.guild_id is None
            or interaction.guild_id not in self._allowed_guild_ids
            or interaction.user.id not in self._admin_user_ids
        ):
            await self._respond(interaction, "你沒有查看或修改他人記憶的權限")
            return None
        return str(interaction.guild_id), str(interaction.user.id)

    @staticmethod
    async def _respond(interaction: discord.Interaction, content: str) -> None:
        """所有記憶管理結果都以私密訊息回覆並停用提及。"""

        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _MemoryMenuView(discord.ui.View):
    def __init__(self, *, service: PersonalMemoryService, guild_id: str, user_id: str) -> None:
        super().__init__(timeout=600)
        self._service = service
        self._guild_id = guild_id
        self._user_id = user_id
        self.add_item(_MemoryActionSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (
            interaction.guild_id == int(self._guild_id)
            and str(interaction.user.id) == self._user_id
        ):
            return True
        await _component_respond(interaction, "這不是你的記憶操作選單")
        return False


class _MemoryActionSelect(discord.ui.Select[_MemoryMenuView]):
    def __init__(self) -> None:
        super().__init__(
            placeholder="選擇操作…",
            options=(
                discord.SelectOption(label="查看記憶", value="view"),
                discord.SelectOption(label="新增記憶", value="create"),
                discord.SelectOption(label="修改記憶", value="edit"),
                discord.SelectOption(label="刪除記憶", value="delete"),
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _MemoryMenuView):
            await _component_respond(interaction, "記憶選單已失效，請重新開啟")
            return
        action = self.values[0]
        try:
            if action == "create":
                await interaction.response.send_modal(
                    _MemoryContentModal(
                        service=view._service,
                        guild_id=view._guild_id,
                        user_id=view._user_id,
                    )
                )
                return
            memories = await view._service.list_own(
                guild_id=view._guild_id,
                user_id=view._user_id,
            )
        except Exception:
            await _component_respond(interaction, "讀取記憶失敗，請稍後再試")
            return
        if action == "view":
            await interaction.response.edit_message(
                content=_format_memories(memories),
                view=None,
            )
            return
        if not memories:
            await interaction.response.edit_message(content="目前沒有可操作的記憶", view=None)
            return
        await interaction.response.edit_message(
            content="選擇一筆記憶：",
            view=_MemoryPickView(
                service=view._service,
                guild_id=view._guild_id,
                user_id=view._user_id,
                memories=memories[:25],
                action=action,
            ),
        )


class _MemoryPickView(_MemoryMenuView):
    def __init__(
        self,
        *,
        service: PersonalMemoryService,
        guild_id: str,
        user_id: str,
        memories: tuple[object, ...],
        action: str,
    ) -> None:
        discord.ui.View.__init__(self, timeout=600)
        self._service = service
        self._guild_id = guild_id
        self._user_id = user_id
        self.add_item(_MemoryPickSelect(memories=memories, action=action))


class _MemoryPickSelect(discord.ui.Select[_MemoryPickView]):
    def __init__(self, *, memories: tuple[object, ...], action: str) -> None:
        self._action = action
        super().__init__(
            placeholder="選擇記憶…",
            options=tuple(
                discord.SelectOption(
                    label=f"#{memory.id} {memory.content}"[:100],
                    value=str(memory.id),
                )
                for memory in memories
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, _MemoryPickView):
            await _component_respond(interaction, "記憶選單已失效，請重新開啟")
            return
        memory_id = int(self.values[0])
        if self._action == "edit":
            await interaction.response.send_modal(
                _MemoryContentModal(
                    service=view._service,
                    guild_id=view._guild_id,
                    user_id=view._user_id,
                    memory_id=memory_id,
                )
            )
            return
        try:
            deleted = await view._service.delete_manual(
                guild_id=view._guild_id,
                user_id=view._user_id,
                memory_id=memory_id,
            )
        except Exception:
            await _component_respond(interaction, "刪除記憶失敗，請稍後再試")
            return
        await interaction.response.edit_message(
            content=f"已刪除記憶 #{memory_id}" if deleted else "找不到可刪除的記憶",
            view=None,
        )


class _MemoryContentModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        service: PersonalMemoryService,
        guild_id: str,
        user_id: str,
        memory_id: int | None = None,
    ) -> None:
        super().__init__(title="修改記憶" if memory_id is not None else "新增記憶")
        self._service = service
        self._guild_id = guild_id
        self._user_id = user_id
        self._memory_id = memory_id
        self.content = discord.ui.TextInput(label="記憶內容", max_length=200)
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            if self._memory_id is None:
                result = await self._service.create_manual(
                    guild_id=self._guild_id,
                    user_id=self._user_id,
                    content=self.content.value,
                )
                message = f"已新增記憶 #{result.memory.id}"
            else:
                memory = await self._service.update_manual(
                    guild_id=self._guild_id,
                    user_id=self._user_id,
                    memory_id=self._memory_id,
                    content=self.content.value,
                )
                message = (
                    f"已修改記憶 #{self._memory_id}" if memory is not None else "找不到可修改的記憶"
                )
        except (InvalidMemoryContentError, SensitiveMemoryContentError) as error:
            await _component_respond(interaction, str(error))
            return
        except MemoryConflictError:
            await _component_respond(interaction, "記憶內容與既有資料衝突")
            return
        except Exception:
            await _component_respond(interaction, "儲存記憶失敗，請稍後再試")
            return
        await _component_respond(interaction, message)


async def _component_respond(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
        return
    await interaction.response.send_message(content, ephemeral=True)


def _format_memories(memories: tuple[object, ...]) -> str:
    if not memories:
        return "目前沒有記憶"
    return "你的記憶：\n" + "\n".join(f"#{memory.id}　{memory.content}" for memory in memories)
