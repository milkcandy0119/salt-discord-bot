"""將持久化提醒只透過 Discord 私訊交付。"""

from __future__ import annotations

import discord

from app.reminders.dispatcher import ReminderDeliveryError
from app.storage.reminders import Reminder


class DiscordReminderSender:
    """私訊失敗不會改在公開頻道補發。"""

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def send(self, reminder: Reminder) -> None:
        """取得使用者後傳送停用 mentions 的固定提醒文字。"""

        try:
            user_id = int(reminder.user_id)
            user = self._client.get_user(user_id) or await self._client.fetch_user(user_id)
            await user.send(
                f"提醒時間到了：{reminder.content}",
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
