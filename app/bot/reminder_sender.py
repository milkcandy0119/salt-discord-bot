"""Deliver persisted reminders as private Discord embeds."""

from __future__ import annotations

from datetime import UTC
from zoneinfo import ZoneInfo

import discord

from app.reminders.dispatcher import ReminderDeliveryError
from app.storage.reminders import Reminder


class DiscordReminderSender:
    """DM reminders only; delivery failures never fall back to a public channel."""

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def send(self, reminder: Reminder) -> None:
        """Send one reminder as an embed without resolving user mentions."""

        try:
            user_id = int(reminder.user_id)
            user = self._client.get_user(user_id) or await self._client.fetch_user(user_id)
            await user.send(
                embed=self._build_embed(reminder),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except (discord.Forbidden, discord.NotFound) as error:
            raise ReminderDeliveryError(
                "discord_dm_unavailable",
                retryable=False,
            ) from error
        except discord.DiscordException as error:
            raise ReminderDeliveryError(
                "discord_delivery_error",
                retryable=True,
            ) from error

    @staticmethod
    def _build_embed(reminder: Reminder) -> discord.Embed:
        local = (
            reminder.due_at.replace(tzinfo=UTC)
            if reminder.due_at.tzinfo is None
            else reminder.due_at.astimezone(UTC)
        ).astimezone(ZoneInfo(reminder.timezone_name))
        embed = discord.Embed(
            title="⏰ 提醒時間到了",
            description=reminder.content,
            colour=discord.Colour.blurple(),
            timestamp=local,
        )
        recurrence_label = {
            "once": "單次",
            "daily": "每天",
            "weekly": "每週",
            "interval": f"每 {reminder.interval_days} 天",
        }.get(reminder.recurrence_kind, reminder.recurrence_kind)
        embed.add_field(name="提醒規則", value=recurrence_label, inline=False)
        embed.add_field(
            name="設定時間",
            value=f"{local:%Y-%m-%d %H:%M} {reminder.timezone_name}",
            inline=False,
        )
        embed.set_footer(text=f"提醒 #{reminder.id}")
        return embed
