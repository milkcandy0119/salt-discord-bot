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


class PersonalMemoryCommandGroup(app_commands.Group):
    """將使用者身分固定取自 Interaction，命令不接受目標 user ID。"""

    def __init__(
        self,
        *,
        service: PersonalMemoryService,
        allowed_guild_ids: frozenset[int],
    ) -> None:
        super().__init__(name="memory", description="管理 Salt 對你自己的基本記憶")
        self._service = service
        self._allowed_guild_ids = allowed_guild_ids

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
                    f"已更新記憶 #{memory.id}"
                    if memory is not None
                    else "找不到屬於你的這筆記憶"
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

    async def _owner(
        self,
        interaction: discord.Interaction,
    ) -> tuple[str, str] | None:
        """從 Discord Interaction 取得固定擁有者，不接受命令參數代替。"""

        if (
            interaction.guild_id is None
            or interaction.guild_id not in self._allowed_guild_ids
        ):
            await self._respond(interaction, "這個伺服器不能使用個人記憶功能")
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
