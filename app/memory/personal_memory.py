"""只擷取使用者明確要求保存的第一人稱個人記憶。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.security.sensitive_filter import SensitiveFilter
from app.storage.personal_memories import (
    MemoryConflictError,
    MemorySaveResult,
    PersonalMemory,
    PersonalMemoryRepository,
)

MAX_MEMORY_CHARACTERS = 200
_EXPLICIT_MEMORY_PATTERN = re.compile(
    r"^\s*(?:(?:請|拜託|你|salt|ソルト|<@!?\d+>)\s*){0,3}"
    r"(?:記得|記住)\s*(?:一下\s*)?[，,：:]?\s*我(?P<fact>.+?)\s*[。.!！]?\s*$",
    re.IGNORECASE,
)


class InvalidMemoryContentError(ValueError):
    """記憶內容為空白、過長或不符合基本格式。"""


class SensitiveMemoryContentError(ValueError):
    """記憶內容含可能的祕密，不允許保存。"""


@dataclass(frozen=True, slots=True)
class MemoryCaptureOutcome:
    """日常聊天免費擷取的結果。"""

    status: str
    save_result: MemorySaveResult | None = None


class PersonalMemoryService:
    """共用日常事件與 Slash Command 的內容安全規則。"""

    def __init__(
        self,
        repository: PersonalMemoryRepository,
        *,
        sensitive_filter: SensitiveFilter,
    ) -> None:
        self._repository = repository
        self._sensitive_filter = sensitive_filter

    async def capture_explicit_message(
        self,
        *,
        guild_id: str,
        user_id: str,
        message_id: str,
        content: str,
    ) -> MemoryCaptureOutcome:
        """用本機規則擷取「請記得我……」，不呼叫任何 AI。"""

        match = _EXPLICIT_MEMORY_PATTERN.fullmatch(content)
        if match is None:
            return MemoryCaptureOutcome("not_memory_event")
        fact = f"我{match.group('fact').strip()}"
        try:
            validated = self.validate_content(fact)
        except SensitiveMemoryContentError:
            return MemoryCaptureOutcome("blocked_sensitive")
        except InvalidMemoryContentError:
            return MemoryCaptureOutcome("invalid_content")
        result = await self._repository.create(
            guild_id=guild_id,
            user_id=user_id,
            content=validated,
            source_type="chat",
            source_message_id=message_id,
        )
        return MemoryCaptureOutcome(
            "created" if result.created else "duplicate",
            result,
        )

    async def create_manual(
        self,
        *,
        guild_id: str,
        user_id: str,
        content: str,
    ) -> MemorySaveResult:
        """由 Slash Command 建立目前使用者自己的記憶。"""

        return await self._repository.create(
            guild_id=guild_id,
            user_id=user_id,
            content=self.validate_content(content),
            source_type="slash",
        )

    async def update_manual(
        self,
        *,
        guild_id: str,
        user_id: str,
        memory_id: int,
        content: str,
    ) -> PersonalMemory | None:
        """由 Slash Command 修改目前使用者自己的指定記憶。"""

        return await self._repository.update_own(
            guild_id=guild_id,
            user_id=user_id,
            memory_id=memory_id,
            content=self.validate_content(content),
        )

    async def delete_manual(
        self,
        *,
        guild_id: str,
        user_id: str,
        memory_id: int,
    ) -> bool:
        """由 Slash Command 刪除目前使用者自己的指定記憶。"""

        return await self._repository.delete_own(
            guild_id=guild_id,
            user_id=user_id,
            memory_id=memory_id,
        )

    async def list_own(
        self,
        *,
        guild_id: str,
        user_id: str,
    ) -> tuple[PersonalMemory, ...]:
        """列出目前使用者在此伺服器的記憶。"""

        return await self._repository.list_for_user(
            guild_id=guild_id,
            user_id=user_id,
        )

    def validate_content(self, content: str) -> str:
        """拒絕空白、過長及任何可能含祕密的記憶。"""

        cleaned = re.sub(r"\s+", " ", content).strip()
        if not cleaned or len(cleaned) > MAX_MEMORY_CHARACTERS:
            raise InvalidMemoryContentError(
                f"記憶內容必須介於 1 到 {MAX_MEMORY_CHARACTERS} 個字元"
            )
        if self._sensitive_filter.scan(cleaned).is_sensitive:
            raise SensitiveMemoryContentError("可能含敏感資料，未保存為記憶")
        return cleaned


__all__ = [
    "InvalidMemoryContentError",
    "MemoryCaptureOutcome",
    "MemoryConflictError",
    "PersonalMemoryService",
    "SensitiveMemoryContentError",
]
