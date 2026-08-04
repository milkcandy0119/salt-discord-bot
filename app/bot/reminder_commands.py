"""本人限定且全部採私密回覆的提醒與時區 Slash Commands。"""

from __future__ import annotations

import discord
from discord import app_commands

from app.reminders.service import (
    InvalidReminderError,
    ReminderService,
    SensitiveReminderError,
)


class _GuildCommandGroup(app_commands.Group):
    """提醒相關群組共用的伺服器白名單檢查。"""

    def __init__(self, *, allowed_guild_ids: frozenset[int], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._allowed_guild_ids = allowed_guild_ids

    async def _owner(
        self,
        interaction: discord.Interaction,
    ) -> tuple[str, str] | None:
        if (
            interaction.guild_id is None
            or interaction.guild_id not in self._allowed_guild_ids
        ):
            await self._respond(interaction, "這個伺服器不能使用提醒功能")
            return None
        return str(interaction.guild_id), str(interaction.user.id)

    @staticmethod
    async def _respond(interaction: discord.Interaction, content: str) -> None:
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ReminderCommandGroup(_GuildCommandGroup):
    """建立、查看與取消目前使用者自己的提醒。"""

    def __init__(
        self,
        *,
        service: ReminderService,
        allowed_guild_ids: frozenset[int],
    ) -> None:
        super().__init__(
            name="remind",
            description="建立與管理自己的私訊提醒",
            allowed_guild_ids=allowed_guild_ids,
        )
        self._service = service

    @app_commands.command(name="create", description="以明確日期與時間建立私訊提醒")
    @app_commands.describe(
        date="日期，格式 YYYY-MM-DD",
        time="時間，格式 HH:MM",
        content="提醒內容，最多 500 字",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        date: str,
        time: str,
        content: str,
    ) -> None:
        """建立目前使用者自己的持久化提醒。"""

        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        try:
            reminder = await self._service.create(
                guild_id=guild_id,
                user_id=user_id,
                date_text=date,
                time_text=time,
                content=content,
            )
        except (InvalidReminderError, SensitiveReminderError) as error:
            await self._respond(interaction, str(error))
            return
        due_text = self._service.format_due_at(reminder)
        await self._respond(
            interaction,
            f"提醒已建立，編號 #{reminder.id}\n時間：{due_text}\n到期後只會私訊你",
        )

    @app_commands.command(name="list", description="私密查看自己的待處理提醒")
    async def list_reminders(self, interaction: discord.Interaction) -> None:
        """列出目前使用者尚未完成的提醒。"""

        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        reminders = await self._service.list_own(
            guild_id=guild_id,
            user_id=user_id,
        )
        if not reminders:
            await self._respond(interaction, "你目前沒有待處理提醒")
            return
        lines = ["你的待處理提醒："]
        for reminder in reminders:
            line = (
                f"#{reminder.id}　{self._service.format_due_at(reminder)}　"
                f"{reminder.content}"
            )
            if sum(len(item) + 1 for item in (*lines, line)) > 1_850:
                lines.append("……其餘提醒未顯示")
                break
            lines.append(line)
        await self._respond(interaction, "\n".join(lines))

    @app_commands.command(name="cancel", description="取消自己的指定提醒")
    @app_commands.describe(reminder_id="要取消的提醒編號")
    async def cancel(
        self,
        interaction: discord.Interaction,
        reminder_id: int,
    ) -> None:
        """只有提醒擁有者可以取消尚未派送的提醒。"""

        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        cancelled = await self._service.cancel(
            guild_id=guild_id,
            user_id=user_id,
            reminder_id=reminder_id,
        )
        await self._respond(
            interaction,
            f"已取消提醒 #{reminder_id}"
            if cancelled
            else "找不到屬於你的待處理提醒",
        )


class TimezoneCommandGroup(_GuildCommandGroup):
    """查看與設定目前使用者自己的 IANA 時區。"""

    def __init__(
        self,
        *,
        service: ReminderService,
        allowed_guild_ids: frozenset[int],
    ) -> None:
        super().__init__(
            name="timezone",
            description="查看或設定自己的提醒時區",
            allowed_guild_ids=allowed_guild_ids,
        )
        self._service = service

    @app_commands.command(name="view", description="查看目前提醒時區")
    async def view(self, interaction: discord.Interaction) -> None:
        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        timezone_name = await self._service.get_timezone(
            guild_id=guild_id,
            user_id=user_id,
        )
        await self._respond(interaction, f"你目前的提醒時區是 {timezone_name}")

    @app_commands.command(name="set", description="設定 IANA 提醒時區")
    @app_commands.describe(timezone="例如 Asia/Taipei、Asia/Tokyo")
    async def set_timezone(
        self,
        interaction: discord.Interaction,
        timezone: str,
    ) -> None:
        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        try:
            timezone_name = await self._service.set_timezone(
                guild_id=guild_id,
                user_id=user_id,
                timezone_name=timezone,
            )
        except InvalidReminderError as error:
            await self._respond(interaction, str(error))
            return
        await self._respond(interaction, f"提醒時區已設定為 {timezone_name}")
