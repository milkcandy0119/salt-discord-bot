"""以 Discord REST 歷史端點提供唯讀分析資料。"""

from __future__ import annotations

from datetime import datetime

import discord

from app.history.analyzer import HistoricalMessage, HistoryReadResult


class DiscordHistorySource:
    """只登入 Discord 並讀取白名單頻道，不傳送或修改任何訊息。"""

    def __init__(self, *, bot_token: str, allowed_guild_ids: frozenset[int]) -> None:
        if not bot_token:
            raise ValueError("Discord bot token 不得為空")
        self._bot_token = bot_token
        self._allowed_guild_ids = allowed_guild_ids

    async def read(
        self,
        *,
        channel_ids: frozenset[int],
        limit_per_channel: int,
        after: datetime | None,
    ) -> HistoryReadResult:
        """逐頻道讀取最多上限加一則，用額外一則判斷是否截斷。"""

        intents = discord.Intents.none()
        client = discord.Client(intents=intents)
        messages: list[HistoricalMessage] = []
        truncated: list[str] = []
        try:
            await client.login(self._bot_token)
            for channel_id in sorted(channel_ids):
                channel = await client.fetch_channel(channel_id)
                guild = getattr(channel, "guild", None)
                if guild is None or guild.id not in self._allowed_guild_ids:
                    raise PermissionError("歷史頻道不屬於允許的 Discord 伺服器")
                if not isinstance(channel, discord.abc.Messageable):
                    raise TypeError("設定的 Discord channel 不支援訊息歷史")
                fetched = [
                    message
                    async for message in channel.history(
                        limit=limit_per_channel + 1,
                        oldest_first=True,
                        after=after,
                    )
                ]
                if len(fetched) > limit_per_channel:
                    truncated.append(str(channel_id))
                    fetched = fetched[:limit_per_channel]
                for message in fetched:
                    if message.type not in {
                        discord.MessageType.default,
                        discord.MessageType.reply,
                    }:
                        continue
                    reference = message.reference
                    replied_to_message_id = (
                        str(reference.message_id)
                        if reference is not None and reference.message_id is not None
                        else None
                    )
                    messages.append(
                        HistoricalMessage(
                            discord_message_id=str(message.id),
                            guild_id=str(guild.id),
                            channel_id=str(channel_id),
                            author_id=str(message.author.id),
                            author_display_name=getattr(
                                message.author, "display_name", None
                            ),
                            content=message.content,
                            created_at=message.created_at,
                            replied_to_message_id=replied_to_message_id,
                            is_bot=message.author.bot,
                            sticker_names=tuple(
                                sticker.name
                                for sticker in message.stickers
                                if isinstance(sticker.name, str)
                            ),
                            attachment_count=len(message.attachments),
                            attachment_bytes=sum(
                                attachment.size for attachment in message.attachments
                            ),
                        )
                    )
        finally:
            await client.close()
        return HistoryReadResult(tuple(messages), tuple(truncated))
