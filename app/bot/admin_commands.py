"""固定管理員 ID 才能使用的私密健康與用量查詢。"""

from __future__ import annotations

import discord
from discord import app_commands

from app.ai.budget_manager import BudgetManager
from app.storage.admin_audit import AdminAuditRepository
from app.storage.background_memory import BackgroundMemoryRepository
from app.storage.reminders import ReminderRepository


class BotAdminCommandGroup(app_commands.Group):
    """只顯示彙總狀態，不輸出訊息、摘要、記憶或提醒內容。"""

    def __init__(
        self,
        *,
        client: discord.Client,
        budget_manager: BudgetManager,
        background_repository: BackgroundMemoryRepository,
        reminder_repository: ReminderRepository,
        audit_repository: AdminAuditRepository,
        allowed_guild_ids: frozenset[int],
        admin_user_ids: frozenset[int],
    ) -> None:
        super().__init__(name="bot", description="Discord 助手管理狀態")
        self._client = client
        self._budget_manager = budget_manager
        self._background_repository = background_repository
        self._reminder_repository = reminder_repository
        self._audit_repository = audit_repository
        self._allowed_guild_ids = allowed_guild_ids
        self._admin_user_ids = admin_user_ids

    @app_commands.command(name="status", description="私密查看健康、用量與佇列狀態")
    async def status(self, interaction: discord.Interaction) -> None:
        """經固定 ID 驗證後讀取完全免費的本機彙總。"""

        if (
            interaction.guild_id is None
            or interaction.guild_id not in self._allowed_guild_ids
            or interaction.user.id not in self._admin_user_ids
        ):
            await self._respond(interaction, "你沒有查看機器人管理狀態的權限")
            return
        snapshot = await self._budget_manager.get_snapshot()
        background_jobs = await self._background_repository.status_counts()
        reminders = await self._reminder_repository.status_counts()
        notifications = await self._budget_manager.get_notification_statuses()
        await self._audit_repository.record(
            guild_id=str(interaction.guild_id),
            actor_user_id=str(interaction.user.id),
            action="bot_status_view",
        )
        latency_ms = round(self._client.latency * 1_000)
        await self._respond(
            interaction,
            "\n".join(
                (
                    f"連線：{'ready' if self._client.is_ready() else 'not_ready'}",
                    f"Discord 延遲：約 {latency_ms} ms",
                    "全域用量："
                    f"spent={snapshot.global_spent_microusd} μUSD "
                    f"reserved={snapshot.global_reserved_microusd} μUSD",
                    "背景用量："
                    f"spent={snapshot.background_spent_microusd} μUSD "
                    f"reserved={snapshot.background_reserved_microusd} μUSD",
                    f"背景工作：{background_jobs}",
                    f"提醒：{reminders}",
                    f"70%／90% 通知：{notifications}",
                )
            ),
        )

    @staticmethod
    async def _respond(interaction: discord.Interaction, content: str) -> None:
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
