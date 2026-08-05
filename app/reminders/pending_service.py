"""將確認過的待辦安全交給既有提醒服務。"""

from __future__ import annotations

from app.reminders.natural_language import NaturalLanguageReminderParser, ParsedReminderRequest
from app.reminders.service import InvalidReminderError, ReminderService
from app.storage.pending_actions import PendingAction, PendingActionRepository
from app.storage.reminders import Reminder


class PendingReminderService:
    """自然語言只產生候選，確認後才建立提醒。"""

    def __init__(
        self,
        *,
        pending_repository: PendingActionRepository,
        reminder_service: ReminderService,
        parser: NaturalLanguageReminderParser | None = None,
    ) -> None:
        self._pending_repository = pending_repository
        self._reminder_service = reminder_service
        self._parser = parser or NaturalLanguageReminderParser()

    async def propose(
        self, *, guild_id: str, channel_id: str, user_id: str, text: str
    ) -> tuple[PendingAction, ParsedReminderRequest]:
        timezone_name = await self._reminder_service.require_timezone(
            guild_id=guild_id, user_id=user_id
        )
        parsed = self._parser.parse(text=text, timezone_name=timezone_name)
        action = await self._pending_repository.create(
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=user_id,
            action_type="create_reminder",
            parsed_parameters={
                "date": parsed.date_text,
                "time": parsed.time_text,
                "content": parsed.content,
            },
        )
        return action, parsed

    async def confirm(
        self, *, action_id: int, guild_id: str, channel_id: str, user_id: str
    ) -> Reminder | None:
        action = await self._pending_repository.claim_for_execution(
            action_id=action_id, guild_id=guild_id, channel_id=channel_id, user_id=user_id
        )
        if action is None:
            return None
        try:
            if action.action_type != "create_reminder":
                raise InvalidReminderError("這個待確認動作類型不受支援")
            parameters = action.parsed_parameters
            reminder = await self._reminder_service.create(
                guild_id=guild_id,
                user_id=user_id,
                date_text=parameters["date"],
                time_text=parameters["time"],
                content=parameters["content"],
            )
        except Exception:
            await self._pending_repository.finish(action_id=action.id, succeeded=False)
            raise
        await self._pending_repository.finish(action_id=action.id, succeeded=True)
        return reminder
