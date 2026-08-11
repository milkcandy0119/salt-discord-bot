"""Private reminder commands and Discord component-based reminder flows."""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from app.reminders.service import (
    InvalidReminderError,
    ReminderService,
    SensitiveReminderError,
)
from app.storage.reminders import Reminder

LOGGER = logging.getLogger(__name__)


class ReminderInteractionError(RuntimeError):
    """A reminder component interaction could not be completed safely."""


class _GuildCommandGroup(app_commands.Group):
    def __init__(self, *, allowed_guild_ids: frozenset[int], **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._allowed_guild_ids = allowed_guild_ids

    async def _owner(self, interaction: discord.Interaction) -> tuple[str, str] | None:
        if (
            interaction.guild_id is None
            or interaction.guild_id not in self._allowed_guild_ids
        ):
            await self._respond(interaction, "這個指令只能在允許的伺服器使用")
            return None
        return str(interaction.guild_id), str(interaction.user.id)

    @staticmethod
    async def _respond(interaction: discord.Interaction, content: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(
                content,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class _OwnedReminderView(discord.ui.View):
    def __init__(self, *, service: ReminderService, guild_id: str, user_id: str) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.guild_id = guild_id
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id == int(self.guild_id) and str(interaction.user.id) == self.user_id:
            return True
        await _GuildCommandGroup._respond(interaction, "這不是你的提醒操作選單")
        return False

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        del item
        LOGGER.exception("Reminder component interaction failed", exc_info=error)
        try:
            await _GuildCommandGroup._respond(
                interaction,
                "提醒操作發生錯誤，請重新開啟選單後再試一次",
            )
        except discord.DiscordException:
            LOGGER.exception("Could not report reminder component error")


class _ReminderForm(discord.ui.Modal):
    def __init__(
        self,
        *,
        service: ReminderService,
        guild_id: str,
        user_id: str,
        mode: str,
    ) -> None:
        titles = {
            "once": "建立一次提醒",
            "daily": "建立每日提醒",
            "weekly": "建立每週提醒",
            "interval": "建立固定間隔提醒",
        }
        if mode not in titles:
            raise ReminderInteractionError(f"Unknown reminder creation mode: {mode}")
        super().__init__(title=titles[mode])
        self._service = service
        self._guild_id = guild_id
        self._user_id = user_id
        self._mode = mode
        self._date = discord.ui.TextInput(
            label="日期",
            placeholder="YYYY-MM-DD",
            max_length=10,
            required=True,
        )
        self._weekdays = discord.ui.TextInput(
            label="星期",
            placeholder="mon,wed,fri",
            max_length=27,
            required=True,
        )
        self._every = discord.ui.TextInput(
            label="間隔",
            placeholder="3d（每 3 天）",
            max_length=5,
            required=True,
        )
        self._start_date = discord.ui.TextInput(
            label="起始日期",
            placeholder="YYYY-MM-DD",
            max_length=10,
            required=True,
        )
        self._time = discord.ui.TextInput(
            label="時間",
            placeholder="HH:MM（24 小時制）",
            max_length=5,
            required=True,
        )
        self._content = discord.ui.TextInput(
            label="提醒內容",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=True,
        )
        if mode == "once":
            self.add_item(self._date)
        elif mode == "weekly":
            self.add_item(self._weekdays)
        elif mode == "interval":
            self.add_item(self._every)
            self.add_item(self._start_date)
        self.add_item(self._time)
        self.add_item(self._content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            reminder = await self._create_reminder()
        except (InvalidReminderError, SensitiveReminderError) as error:
            await _GuildCommandGroup._respond(interaction, str(error))
            return
        except Exception as error:
            LOGGER.exception("Reminder form submission failed")
            raise ReminderInteractionError("Could not create reminder") from error
        await _GuildCommandGroup._respond(interaction, _confirmation(self._service, reminder))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        LOGGER.exception("Reminder modal interaction failed", exc_info=error)
        try:
            await _GuildCommandGroup._respond(
                interaction,
                "建立提醒時發生錯誤，請重新開啟選單後再試一次",
            )
        except discord.DiscordException:
            LOGGER.exception("Could not report reminder modal error")

    async def _create_reminder(self) -> Reminder:
        if self._mode == "once":
            return await self._service.create(
                guild_id=self._guild_id,
                user_id=self._user_id,
                date_text=self._date.value,
                time_text=self._time.value,
                content=self._content.value,
            )
        if self._mode == "daily":
            return await self._service.create_daily(
                guild_id=self._guild_id,
                user_id=self._user_id,
                time_text=self._time.value,
                content=self._content.value,
            )
        if self._mode == "weekly":
            return await self._service.create_weekly(
                guild_id=self._guild_id,
                user_id=self._user_id,
                weekdays_text=self._weekdays.value,
                time_text=self._time.value,
                content=self._content.value,
            )
        return await self._service.create_interval(
            guild_id=self._guild_id,
            user_id=self._user_id,
            every_text=self._every.value,
            start_date_text=self._start_date.value,
            time_text=self._time.value,
            content=self._content.value,
        )


class _CreateModeSelect(discord.ui.Select["_CreateReminderView"]):
    def __init__(self) -> None:
        super().__init__(
            placeholder="選擇提醒類型…",
            min_values=1,
            max_values=1,
            options=(
                discord.SelectOption(label="一次提醒", value="once", description="指定日期與時間"),
                discord.SelectOption(label="每天", value="daily", description="每天固定時間"),
                discord.SelectOption(label="每週", value="weekly", description="指定星期幾"),
                discord.SelectOption(label="固定間隔", value="interval", description="例如每 3 天"),
            ),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            view = self.view
            if not isinstance(view, _CreateReminderView):
                raise ReminderInteractionError("Create reminder view is unavailable")
            await interaction.response.send_modal(
                _ReminderForm(
                    service=view.service,
                    guild_id=view.guild_id,
                    user_id=view.user_id,
                    mode=self.values[0],
                )
            )
        except (IndexError, ReminderInteractionError) as error:
            await _GuildCommandGroup._respond(interaction, str(error))


class _CreateReminderView(_OwnedReminderView):
    def __init__(self, *, service: ReminderService, guild_id: str, user_id: str) -> None:
        super().__init__(service=service, guild_id=guild_id, user_id=user_id)
        self.add_item(_CreateModeSelect())


class _ReminderSelect(discord.ui.Select["_BulkReminderView"]):
    def __init__(self, *, service: ReminderService, reminders: tuple[Reminder, ...]) -> None:
        options = tuple(_reminder_option(service, reminder) for reminder in reminders)
        super().__init__(
            placeholder="選擇要管理的提醒…",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            view = self.view
            if not isinstance(view, _BulkReminderView):
                raise ReminderInteractionError("Reminder management view is unavailable")
            view.selected_ids = tuple(int(value) for value in self.values)
            view.edit_content.disabled = False
            view.delete_reminders.disabled = False
            await interaction.response.edit_message(view=view)
        except (ValueError, ReminderInteractionError) as error:
            await _GuildCommandGroup._respond(interaction, str(error))


class _BulkContentModal(discord.ui.Modal, title="批量修改提醒內容"):
    content = discord.ui.TextInput(
        label="新的提醒內容",
        style=discord.TextStyle.paragraph,
        max_length=500,
        required=True,
    )

    def __init__(
        self,
        *,
        service: ReminderService,
        guild_id: str,
        user_id: str,
        reminder_ids: tuple[int, ...],
    ) -> None:
        super().__init__()
        self._service = service
        self._guild_id = guild_id
        self._user_id = user_id
        self._reminder_ids = reminder_ids

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            updated = await self._service.update_many_content(
                guild_id=self._guild_id,
                user_id=self._user_id,
                reminder_ids=self._reminder_ids,
                content=self.content.value,
            )
        except (InvalidReminderError, SensitiveReminderError) as error:
            await _GuildCommandGroup._respond(interaction, str(error))
            return
        except Exception as error:
            LOGGER.exception("Bulk reminder content update failed")
            raise ReminderInteractionError("Could not update reminder content") from error
        await _GuildCommandGroup._respond(interaction, f"已更新 {updated} 個提醒的內容")

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        LOGGER.exception("Bulk reminder content modal failed", exc_info=error)
        try:
            await _GuildCommandGroup._respond(interaction, "批量編輯失敗，請重新開啟管理選單")
        except discord.DiscordException:
            LOGGER.exception("Could not report bulk edit error")


class _BulkReminderView(_OwnedReminderView):
    def __init__(
        self,
        *,
        service: ReminderService,
        guild_id: str,
        user_id: str,
        reminders: tuple[Reminder, ...],
    ) -> None:
        super().__init__(service=service, guild_id=guild_id, user_id=user_id)
        self.selected_ids: tuple[int, ...] = ()
        self.add_item(_ReminderSelect(service=service, reminders=reminders))

    @discord.ui.button(label="批量改內容", style=discord.ButtonStyle.primary, disabled=True, row=1)
    async def edit_content(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[_BulkReminderView],
    ) -> None:
        del button
        if not self.selected_ids:
            await _GuildCommandGroup._respond(interaction, "請先選擇至少一個提醒")
            return
        await interaction.response.send_modal(
            _BulkContentModal(
                service=self.service,
                guild_id=self.guild_id,
                user_id=self.user_id,
                reminder_ids=self.selected_ids,
            )
        )

    @discord.ui.button(label="批量刪除", style=discord.ButtonStyle.danger, disabled=True, row=1)
    async def delete_reminders(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[_BulkReminderView],
    ) -> None:
        del button
        if not self.selected_ids:
            await _GuildCommandGroup._respond(interaction, "請先選擇至少一個提醒")
            return
        await interaction.response.edit_message(
            content=f"確定要取消 {len(self.selected_ids)} 個提醒嗎？此操作無法復原。",
            view=_DeleteConfirmationView(
                service=self.service,
                guild_id=self.guild_id,
                user_id=self.user_id,
                reminder_ids=self.selected_ids,
            ),
        )


class _DeleteConfirmationView(_OwnedReminderView):
    def __init__(
        self,
        *,
        service: ReminderService,
        guild_id: str,
        user_id: str,
        reminder_ids: tuple[int, ...],
    ) -> None:
        super().__init__(service=service, guild_id=guild_id, user_id=user_id)
        self._reminder_ids = reminder_ids

    @discord.ui.button(label="確認取消", style=discord.ButtonStyle.danger)
    async def confirm(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[_DeleteConfirmationView],
    ) -> None:
        del button
        try:
            cancelled = await self.service.cancel_many(
                guild_id=self.guild_id,
                user_id=self.user_id,
                reminder_ids=self._reminder_ids,
            )
        except InvalidReminderError as error:
            await _GuildCommandGroup._respond(interaction, str(error))
            return
        await interaction.response.edit_message(
            content=f"已取消 {cancelled} 個提醒",
            view=None,
        )

    @discord.ui.button(label="返回", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button[_DeleteConfirmationView],
    ) -> None:
        del button
        await interaction.response.edit_message(content="已取消刪除操作", view=None)


class ReminderCommandGroup(_GuildCommandGroup):
    """The /remind command, including private component-based flows."""

    def __init__(
        self, *, service: ReminderService, allowed_guild_ids: frozenset[int]
    ) -> None:
        super().__init__(
            name="remind",
            description="建立與管理私密提醒",
            allowed_guild_ids=allowed_guild_ids,
        )
        self._service = service

    @app_commands.command(name="create", description="以選單建立提醒")
    async def create(self, interaction: discord.Interaction) -> None:
        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        await self._respond(
            interaction,
            "請選擇提醒類型，接著填寫表單：",
            view=_CreateReminderView(
                service=self._service,
                guild_id=guild_id,
                user_id=user_id,
            ),
        )

    @app_commands.command(name="manage", description="以選單批量編輯或刪除提醒")
    async def manage(self, interaction: discord.Interaction) -> None:
        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        try:
            reminders = await self._service.list_own(
                guild_id=guild_id,
                user_id=user_id,
                limit=25,
            )
        except Exception as error:
            LOGGER.exception("Could not list reminders for bulk management")
            raise ReminderInteractionError("Could not load reminders") from error
        if not reminders:
            await self._respond(interaction, "你目前沒有可管理的提醒")
            return
        await self._respond(
            interaction,
            "最多可選擇 25 個提醒。批量編輯會套用相同的新內容到所有選取項目。",
            view=_BulkReminderView(
                service=self._service,
                guild_id=guild_id,
                user_id=user_id,
                reminders=reminders,
            ),
        )

    @app_commands.command(name="list", description="私密查看自己的待處理提醒")
    async def list_reminders(self, interaction: discord.Interaction) -> None:
        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        try:
            reminders = await self._service.list_own(guild_id=guild_id, user_id=user_id)
        except Exception as error:
            LOGGER.exception("Could not list reminders")
            raise ReminderInteractionError("Could not load reminders") from error
        if not reminders:
            await self._respond(interaction, "你目前沒有待處理提醒")
            return
        lines = ["你的待處理提醒："]
        for reminder in reminders:
            line = (
                f"#{reminder.id}　{self._service.format_recurrence(reminder)}　"
                f"下一次 {self._service.format_due_at(reminder)}　{reminder.content}"
            )
            if sum(len(item) + 1 for item in (*lines, line)) > 1_850:
                lines.append("……其餘提醒未顯示")
                break
            lines.append(line)
        await self._respond(interaction, "\n".join(lines))

    @app_commands.command(name="cancel", description="取消自己的單一提醒")
    @app_commands.describe(reminder_id="提醒編號")
    async def cancel(self, interaction: discord.Interaction, reminder_id: int) -> None:
        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        try:
            cancelled = await self._service.cancel(
                guild_id=guild_id,
                user_id=user_id,
                reminder_id=reminder_id,
            )
        except Exception as error:
            LOGGER.exception("Could not cancel reminder")
            raise ReminderInteractionError("Could not cancel reminder") from error
        await self._respond(
            interaction,
            f"已取消提醒 #{reminder_id}" if cancelled else "找不到可取消的自己的提醒",
        )

    @staticmethod
    async def _respond(
        interaction: discord.Interaction,
        content: str,
        *,
        view: discord.ui.View | None = None,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(
                content,
                ephemeral=True,
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_message(
            content,
            ephemeral=True,
            view=view,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class TimezoneCommandGroup(_GuildCommandGroup):
    def __init__(
        self, *, service: ReminderService, allowed_guild_ids: frozenset[int]
    ) -> None:
        super().__init__(
            name="timezone",
            description="管理提醒使用的時區",
            allowed_guild_ids=allowed_guild_ids,
        )
        self._service = service

    @app_commands.command(name="view", description="查看目前的提醒時區")
    async def view(self, interaction: discord.Interaction) -> None:
        owner = await self._owner(interaction)
        if owner is None:
            return
        guild_id, user_id = owner
        timezone_name = await self._service.get_timezone(guild_id=guild_id, user_id=user_id)
        await self._respond(interaction, f"你的提醒時區：{timezone_name}")

    @app_commands.command(name="set", description="設定 IANA 時區")
    @app_commands.describe(timezone="例如 Asia/Taipei、Asia/Tokyo")
    async def set_timezone(self, interaction: discord.Interaction, timezone: str) -> None:
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


def _confirmation(service: ReminderService, reminder: Reminder) -> str:
    return (
        f"提醒已建立，編號 #{reminder.id}\n"
        f"規則：{service.format_recurrence(reminder)}\n"
        f"下一次：{service.format_due_at(reminder)}\n"
        "到期後只會私訊你"
    )


def _reminder_option(service: ReminderService, reminder: Reminder) -> discord.SelectOption:
    label = (
        f"#{reminder.id} {service.format_recurrence(reminder)} "
        f"{service.format_due_at(reminder)}"
    )
    return discord.SelectOption(
        label=label[:100],
        value=str(reminder.id),
        description=reminder.content.replace("\n", " ")[:100],
    )
