"""固定管理員 ID 才能提交的私密試跑分類評價。"""

from __future__ import annotations

from typing import Literal

import discord
from discord import app_commands

from app.storage.admin_audit import AdminAuditRepository
from app.storage.trial import TrialRepository

FeedbackCategory = Literal[
    "good",
    "too_formal",
    "wrong_memory",
    "unwanted_reply",
    "missed_reply",
    "other",
]


class TrialCommandGroup(app_commands.Group):
    """評價只保存訊息 ID 與固定分類，不複製聊天內容。"""

    def __init__(
        self,
        *,
        repository: TrialRepository,
        audit_repository: AdminAuditRepository,
        allowed_guild_ids: frozenset[int],
        admin_user_ids: frozenset[int],
    ) -> None:
        super().__init__(name="trial", description="階段 9 試跑管理評價")
        self._repository = repository
        self._audit_repository = audit_repository
        self._allowed_guild_ids = allowed_guild_ids
        self._admin_user_ids = admin_user_ids

    @app_commands.command(name="feedback", description="私密標記一則試跑訊息的固定評價")
    @app_commands.describe(
        message_id="Discord 訊息 ID，不要貼訊息內容",
        category="固定評價分類",
    )
    async def feedback(
        self,
        interaction: discord.Interaction,
        message_id: str,
        category: FeedbackCategory,
    ) -> None:
        """只允許固定管理員評價目前伺服器已保存的試跑訊息。"""

        if (
            interaction.guild_id is None
            or interaction.guild_id not in self._allowed_guild_ids
            or interaction.user.id not in self._admin_user_ids
        ):
            await self._respond(interaction, "你沒有提交試跑評價的權限")
            return
        if not message_id.isdecimal() or int(message_id) <= 0:
            await self._respond(interaction, "message_id 必須是 Discord 訊息 ID")
            return
        result = await self._repository.add_feedback(
            guild_id=str(interaction.guild_id),
            actor_user_id=str(interaction.user.id),
            target_message_id=message_id,
            category=category,
        )
        messages = {
            "created": "試跑評價已記錄",
            "duplicate": "相同評價已經記錄過",
            "message_not_found": "找不到目前試跑期間的這則已保存訊息",
            "no_trial": "目前沒有這個伺服器的試跑紀錄",
            "invalid_category": "不支援這個評價分類",
        }
        if result == "created":
            await self._audit_repository.record(
                guild_id=str(interaction.guild_id),
                actor_user_id=str(interaction.user.id),
                action="trial_feedback",
            )
        await self._respond(interaction, messages[result])

    @staticmethod
    async def _respond(interaction: discord.Interaction, content: str) -> None:
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
