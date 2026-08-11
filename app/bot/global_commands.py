"""所有已安裝伺服器可見、免費且不含管理資訊的 Salt 指令。"""

from __future__ import annotations

from typing import Protocol

import discord
from discord import app_commands

from app.ai.persona import Persona


class ClientStatus(Protocol):
    """全域狀態指令需要的最小 Discord 用戶端介面。"""

    latency: float

    def is_ready(self) -> bool: ...


class SaltGlobalCommandGroup(app_commands.Group):
    """不呼叫 AI、不讀取私人資料且不提供管理能力的全域指令。"""

    def __init__(
        self,
        *,
        client: ClientStatus,
        persona: Persona,
        allowed_guild_ids: frozenset[int],
    ) -> None:
        super().__init__(
            name="salt",
            description="Salt 的公開資訊與使用說明",
            guild_only=True,
            allowed_contexts=app_commands.AppCommandContext(
                guild=True,
                dm_channel=False,
                private_channel=False,
            ),
            allowed_installs=app_commands.AppInstallationType(
                guild=True,
                user=False,
            ),
        )
        self._client = client
        self._persona = persona
        self._allowed_guild_ids = allowed_guild_ids

    @app_commands.command(name="about", description="查看 Salt 的身分與版本")
    async def about(self, interaction: discord.Interaction) -> None:
        """顯示可公開的人設來源聲明，不讀取聊天或記憶。"""

        await self._respond(
            interaction,
            "\n".join(
                (
                    f"我是 {self._persona.display_name}，以 maimai 的 Salt 為原型製作的"
                    "非官方 AI 陪伴機器人",
                    "不是 SEGA 官方帳號，也不代表 SEGA",
                    f"人設版本：{self._persona.versioned_id}",
                )
            ),
        )

    @app_commands.command(name="help", description="查看 Salt 的使用方式")
    async def help(self, interaction: discord.Interaction) -> None:
        """依伺服器是否在白名單內提供不含祕密的固定說明。"""

        enabled = (
            interaction.guild_id is not None
            and interaction.guild_id in self._allowed_guild_ids
        )
        lines = [
            "在允許的聊天頻道提及 Salt、回覆 Salt，或使用 Slash Command 即可互動",
            "全域資訊指令：/salt about、/salt help、/salt privacy、/salt ping",
        ]
        if enabled:
            lines.extend(
                (
                    "這個伺服器已啟用完整功能",
                    "個人功能可使用 /memory、/remind 與 /timezone",
                )
            )
        else:
            lines.append("這個伺服器目前只提供全域資訊指令，聊天與個人功能尚未啟用")
        await self._respond(interaction, "\n".join(lines))

    @app_commands.command(name="privacy", description="查看 Salt 的隱私與資料說明")
    async def privacy(self, interaction: discord.Interaction) -> None:
        """提供固定隱私摘要；不查詢或輸出任何人的實際資料。"""

        await self._respond(
            interaction,
            "\n".join(
                (
                    "這個指令不呼叫 AI，也不會顯示聊天、記憶或管理資料",
                    "只有白名單伺服器與頻道的訊息才可能由 Salt 保存；敏感內容會先在本機遮罩",
                    "符合回覆條件的非敏感上下文可能傳送至 OpenAI，並受預算限制",
                    "個人記憶可由本人透過 /memory menu 私密管理",
                )
            ),
        )

    @app_commands.command(name="ping", description="免費查看 Salt 是否在線")
    async def ping(self, interaction: discord.Interaction) -> None:
        """只顯示連線與延遲，不揭露預算、佇列或內部健康資料。"""

        latency_ms = max(0, round(self._client.latency * 1_000))
        status = "online" if self._client.is_ready() else "starting"
        await self._respond(
            interaction,
            f"Salt 狀態：{status}\nDiscord 延遲：約 {latency_ms} ms",
        )

    @staticmethod
    async def _respond(interaction: discord.Interaction, content: str) -> None:
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
