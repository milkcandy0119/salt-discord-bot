"""不含被查看內容的管理操作稽核資料存取。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import AdminAuditEventRecord


class AdminAuditRepository:
    """記錄管理員身分、動作與目標 ID，不保存記憶或提醒內容。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(
        self,
        *,
        guild_id: str,
        actor_user_id: str,
        action: str,
        target_user_id: str | None = None,
        target_record_id: int | None = None,
        now: datetime | None = None,
    ) -> None:
        """保存一筆不含實際資料內容的管理事件。"""

        async with self._session_factory() as session, session.begin():
            session.add(
                AdminAuditEventRecord(
                    guild_id=guild_id,
                    actor_user_id=actor_user_id,
                    action=action,
                    target_user_id=target_user_id,
                    target_record_id=target_record_id,
                    created_at=now or datetime.now(UTC),
                )
            )

    async def count(self, *, action: str | None = None) -> int:
        """供測試與管理狀態查詢事件數，不回傳被操作內容。"""

        statement = select(func.count()).select_from(AdminAuditEventRecord)
        if action is not None:
            statement = statement.where(AdminAuditEventRecord.action == action)
        async with self._session_factory() as session:
            return int(await session.scalar(statement) or 0)
