"""依 Discord channel ID 套用可替換的回覆模式與免費觸發規則。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum


class ChannelMode(StrEnum):
    """白名單頻道支援的運作模式。"""

    NORMAL = "normal"
    COMPANION = "companion"


class TriggerKind(StrEnum):
    """一則訊息觸發回覆的原因。"""

    NONE = "none"
    MENTION = "mention"
    REPLY_TO_BOT = "reply_to_bot"
    COMMAND = "command"
    COMPANION = "companion"


@dataclass(frozen=True, slots=True)
class ReplySignals:
    """不需付費即可取得的回覆判斷訊號。"""

    channel_id: int
    content: str
    mentioned_bot: bool = False
    replied_to_bot: bool = False
    is_command: bool = False
    recent_human_author_ids: frozenset[int] = frozenset()
    bot_spoke_recently: bool = False
    last_companion_reply_at: datetime | None = None
    now: datetime = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class TriggerDecision:
    """免費策略對單一訊息的判斷結果。"""

    should_reply: bool
    kind: TriggerKind
    reason: str


class ChannelModeResolver:
    """只以 channel ID 解析白名單頻道的運作模式。"""

    def __init__(
        self,
        *,
        allowed_channel_ids: frozenset[int],
        companion_channel_ids: frozenset[int],
    ) -> None:
        if not companion_channel_ids.issubset(allowed_channel_ids):
            raise ValueError("陪伴頻道必須同時位於訊息保存白名單")
        self._allowed_channel_ids = allowed_channel_ids
        self._companion_channel_ids = companion_channel_ids

    def resolve(self, channel_id: int) -> ChannelMode | None:
        """傳回頻道模式；非白名單頻道不具有運作模式。"""

        if channel_id not in self._allowed_channel_ids:
            return None
        if channel_id in self._companion_channel_ids:
            return ChannelMode.COMPANION
        return ChannelMode.NORMAL


class ReplyTriggerPolicy:
    """先採明確觸發，再以保守免費規則判斷陪伴回覆。"""

    _QUESTION_PATTERN = re.compile(
        r"[?？]|(?:請問|怎麼|如何|為什麼|哪裡|哪個|是否|能不能|可不可以|幫我|需要建議)"
    )

    def __init__(self, *, companion_cooldown: timedelta) -> None:
        if companion_cooldown < timedelta(0):
            raise ValueError("陪伴模式冷卻時間不得為負數")
        self._companion_cooldown = companion_cooldown

    def decide(self, mode: ChannelMode, signals: ReplySignals) -> TriggerDecision:
        """在不呼叫 AI 的情況下判斷是否建立回覆候選。"""

        # 已註冊 Slash Command 不會進入 on_message；收到這種文字表示 Discord
        # 尚未辨識命令或使用者手動送出了文字，不應交給聊天模型假裝執行。
        if signals.content.lstrip().startswith("/"):
            return TriggerDecision(False, TriggerKind.NONE, "slash_like_text")
        explicit = self._explicit_trigger(signals)
        if explicit is not None:
            return explicit
        if mode is ChannelMode.NORMAL:
            return TriggerDecision(False, TriggerKind.NONE, "normal_requires_explicit_trigger")

        content = signals.content.strip()
        if not content:
            return TriggerDecision(False, TriggerKind.NONE, "empty_content")
        if self._is_in_cooldown(signals):
            return TriggerDecision(False, TriggerKind.NONE, "companion_cooldown")
        if len(signals.recent_human_author_ids) > 1 and not signals.bot_spoke_recently:
            return TriggerDecision(False, TriggerKind.NONE, "multiple_humans_talking")
        if self._QUESTION_PATTERN.search(content):
            return TriggerDecision(True, TriggerKind.COMPANION, "question_or_help_request")
        if signals.bot_spoke_recently and len(content) >= 2:
            return TriggerDecision(True, TriggerKind.COMPANION, "recent_bot_continuation")
        return TriggerDecision(False, TriggerKind.NONE, "no_companion_signal")

    @staticmethod
    def _explicit_trigger(signals: ReplySignals) -> TriggerDecision | None:
        if signals.mentioned_bot:
            return TriggerDecision(True, TriggerKind.MENTION, "bot_mentioned")
        if signals.replied_to_bot:
            return TriggerDecision(True, TriggerKind.REPLY_TO_BOT, "bot_replied_to")
        if signals.is_command:
            return TriggerDecision(True, TriggerKind.COMMAND, "ai_command")
        return None

    def _is_in_cooldown(self, signals: ReplySignals) -> bool:
        if signals.last_companion_reply_at is None:
            return False
        return signals.now - signals.last_companion_reply_at < self._companion_cooldown
