"""階段 6 歷史訊息免費分析命令列入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.ai.budget_manager import BudgetManager, ModelPrice
from app.ai.embedding_service import EmbeddingService, OpenAIEmbeddingProvider
from app.ai.summary_service import OpenAISummaryProvider, SummaryService
from app.config import Settings, get_settings
from app.conversations.segmenter import ConversationSegmenter
from app.history.analyzer import HistoryAnalyzer
from app.history.discord_source import DiscordHistorySource
from app.history.importer import (
    PHASE_SIX_CONFIRMATION,
    ApprovedCostBudgetManager,
    HistoryImporter,
)
from app.security.sensitive_filter import SensitiveFilter
from app.storage.background_memory import BackgroundMemoryRepository
from app.storage.database import Database, upgrade_database
from app.storage.models import (
    ConversationSegmentRecord,
    MessageRecord,
    PaidAiCallRecord,
    PersonalMemoryRecord,
    SegmentSummaryRecord,
    SummaryEmbeddingRecord,
)
from app.storage.repositories import MessageRepository
from app.storage.vector_store import SQLiteVectorStore
from app.workers.background_worker import BackgroundWorker


def _parse_after(value: str) -> datetime:
    """解析 ISO 8601 起始時間，未帶時區時按 UTC 處理。"""

    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="唯讀分析 Discord 白名單歷史，不呼叫 OpenAI 或寫入資料庫。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="輸出免費歷史分析 JSON")
    analyze.add_argument(
        "--limit-per-channel",
        type=int,
        default=10_000,
        help="每個白名單頻道最多讀取的歷史訊息數，預設 10000",
    )
    analyze.add_argument(
        "--after",
        type=_parse_after,
        default=None,
        help="只分析此 ISO 8601 時間之後的訊息，例如 2026-01-01T00:00:00+08:00",
    )
    import_history = subparsers.add_parser(
        "import-history",
        help="在重新估價與批准上限內正式匯入並建立摘要向量",
    )
    import_history.add_argument(
        "--limit-per-channel",
        type=int,
        default=10_000,
        help="每個頻道最多讀取的歷史訊息數，預設 10000",
    )
    import_history.add_argument(
        "--after",
        type=_parse_after,
        default=None,
        help="只讀取這個 ISO 8601 時間之後的訊息",
    )
    import_history.add_argument(
        "--confirmation",
        required=True,
        help=f"必須完全填入：{PHASE_SIX_CONFIRMATION}",
    )
    import_history.add_argument(
        "--maximum-approved-cost-microusd",
        type=int,
        required=True,
        help="本次明確批准的最大微美元成本",
    )
    import_history.add_argument(
        "--approval-baseline-global-committed-microusd",
        type=int,
        required=True,
        help="免費分析報告中的 global_committed_microusd",
    )
    subparsers.add_parser("status", help="唯讀輸出匯入、工作與付費帳本統計")
    return parser


async def _run_analysis(
    settings: Settings,
    *,
    limit_per_channel: int,
    after: datetime | None,
) -> dict[str, object]:
    """建立唯讀服務並回傳不含訊息內容的報告。"""

    if settings.discord_bot_token is None:
        raise ValueError("缺少 DISCORD_BOT_TOKEN，無法讀取 Discord 歷史")
    if not settings.allowed_guild_ids or not settings.allowed_channel_ids:
        raise ValueError("必須先設定 Discord guild/channel 白名單")
    database = Database(settings.database_url)
    try:
        analyzer = HistoryAnalyzer(
            database.session_factory,
            source=DiscordHistorySource(
                bot_token=settings.discord_bot_token.get_secret_value(),
                allowed_guild_ids=settings.allowed_guild_ids,
            ),
            budget_manager=BudgetManager(database.session_factory),
            sensitive_filter=SensitiveFilter(),
            summary_price=ModelPrice(
                model_name=settings.ai_summary_model,
                price_version=settings.ai_summary_price_version,
                input_microusd_per_million_tokens=(
                    settings.ai_summary_input_microusd_per_million_tokens
                ),
                output_microusd_per_million_tokens=(
                    settings.ai_summary_output_microusd_per_million_tokens
                ),
            ),
            embedding_price=ModelPrice(
                model_name=settings.ai_embedding_model,
                price_version=settings.ai_embedding_price_version,
                input_microusd_per_million_tokens=(
                    settings.ai_embedding_input_microusd_per_million_tokens
                ),
                output_microusd_per_million_tokens=0,
            ),
            summary_max_output_tokens=settings.ai_summary_max_output_tokens,
            implicit_continuation_window=timedelta(
                minutes=settings.conversation_implicit_continuation_minutes
            ),
        )
        report = await analyzer.analyze(
            channel_ids=settings.allowed_channel_ids,
            limit_per_channel=limit_per_channel,
            after=after,
        )
        return report.as_dict()
    finally:
        await database.dispose()


async def _run_import(
    settings: Settings,
    *,
    limit_per_channel: int,
    after: datetime | None,
    confirmation: str,
    maximum_approved_cost_microusd: int,
    approval_baseline_global_committed_microusd: int,
) -> dict[str, object]:
    """在明確批准上限內執行可恢復且冪等的正式歷史匯入。"""

    if confirmation != PHASE_SIX_CONFIRMATION:
        raise ValueError("正式匯入確認文字不完全相符")
    if maximum_approved_cost_microusd <= 0:
        raise ValueError("批准費用上限必須大於零")
    if approval_baseline_global_committed_microusd < 0:
        raise ValueError("批准基準承諾成本不得小於零")
    if limit_per_channel <= 0:
        raise ValueError("每個頻道的歷史訊息上限必須大於零")
    if settings.discord_bot_token is None:
        raise ValueError("缺少 DISCORD_BOT_TOKEN，無法讀取 Discord 歷史")
    if settings.openai_api_key is None:
        raise ValueError("缺少 OPENAI_API_KEY，無法執行正式摘要與向量化")
    if not settings.allowed_guild_ids or not settings.allowed_channel_ids:
        raise ValueError("至少需要一個允許的 Discord guild/channel")

    await asyncio.to_thread(upgrade_database, settings.database_url)
    database = Database(settings.database_url)
    try:
        source = DiscordHistorySource(
            bot_token=settings.discord_bot_token.get_secret_value(),
            allowed_guild_ids=settings.allowed_guild_ids,
        )
        sensitive_filter = SensitiveFilter()
        budget_manager = BudgetManager(database.session_factory)
        summary_price = ModelPrice(
            model_name=settings.ai_summary_model,
            price_version=settings.ai_summary_price_version,
            input_microusd_per_million_tokens=(
                settings.ai_summary_input_microusd_per_million_tokens
            ),
            output_microusd_per_million_tokens=(
                settings.ai_summary_output_microusd_per_million_tokens
            ),
        )
        embedding_price = ModelPrice(
            model_name=settings.ai_embedding_model,
            price_version=settings.ai_embedding_price_version,
            input_microusd_per_million_tokens=(
                settings.ai_embedding_input_microusd_per_million_tokens
            ),
            output_microusd_per_million_tokens=0,
        )
        analyzer = HistoryAnalyzer(
            database.session_factory,
            source=source,
            budget_manager=budget_manager,
            sensitive_filter=sensitive_filter,
            summary_price=summary_price,
            embedding_price=embedding_price,
            summary_max_output_tokens=settings.ai_summary_max_output_tokens,
            implicit_continuation_window=timedelta(
                minutes=settings.conversation_implicit_continuation_minutes
            ),
        )
        background_repository = BackgroundMemoryRepository(database.session_factory)
        approved_budget_manager = ApprovedCostBudgetManager(
            budget_manager,
            maximum_approved_cost_microusd,
            approval_baseline_global_committed_microusd,
        )
        api_key = settings.openai_api_key.get_secret_value()
        summary_service = SummaryService(
            provider=OpenAISummaryProvider(api_key),
            repository=background_repository,
            budget_manager=approved_budget_manager,
            price=summary_price,
            sensitive_filter=sensitive_filter,
            maximum_output_tokens=settings.ai_summary_max_output_tokens,
            max_job_attempts=settings.background_job_max_attempts,
        )
        embedding_service = EmbeddingService(
            provider=OpenAIEmbeddingProvider(api_key),
            repository=background_repository,
            vector_store=SQLiteVectorStore(database.session_factory),
            budget_manager=approved_budget_manager,
            price=embedding_price,
            sensitive_filter=sensitive_filter,
            dimensions=settings.ai_embedding_dimensions,
            chunk_characters=settings.ai_embedding_chunk_characters,
            chunk_overlap_characters=settings.ai_embedding_chunk_overlap_characters,
        )
        worker = BackgroundWorker(
            repository=background_repository,
            summary_service=summary_service,
            embedding_service=embedding_service,
            stale_after=timedelta(minutes=settings.background_job_stale_minutes),
            retry_base_delay=timedelta(
                seconds=settings.background_job_retry_base_seconds
            ),
            budget_retry_after=timedelta(
                minutes=settings.background_job_budget_retry_minutes
            ),
            maximum_jobs_per_run=1,
        )
        importer = HistoryImporter(
            database.session_factory,
            source=source,
            analyzer=analyzer,
            message_repository=MessageRepository(database.session_factory),
            segmenter=ConversationSegmenter(
                database.session_factory,
                implicit_continuation_window=timedelta(
                    minutes=settings.conversation_implicit_continuation_minutes
                ),
            ),
            background_repository=background_repository,
            background_worker=worker,
            budget_manager=budget_manager,
            approved_budget_manager=approved_budget_manager,
            sensitive_filter=sensitive_filter,
            max_job_attempts=settings.background_job_max_attempts,
        )
        report = await importer.run(
            channel_ids=settings.allowed_channel_ids,
            limit_per_channel=limit_per_channel,
            after=after,
            confirmation=confirmation,
            maximum_approved_cost_microusd=maximum_approved_cost_microusd,
            approval_baseline_global_committed_microusd=(
                approval_baseline_global_committed_microusd
            ),
        )
        return report.as_dict()
    finally:
        await database.dispose()


async def _run_status(settings: Settings) -> dict[str, object]:
    """唯讀彙總資料狀態，不輸出訊息、摘要或作者內容。"""

    database = Database(settings.database_url)
    try:
        budget = await BudgetManager(database.session_factory).get_snapshot()
        background_repository = BackgroundMemoryRepository(database.session_factory)
        async with database.session_factory() as session:
            message_count = int(
                await session.scalar(select(func.count()).select_from(MessageRecord))
                or 0
            )
            sensitive_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MessageRecord)
                    .where(MessageRecord.is_sensitive.is_(True))
                )
                or 0
            )
            unsegmented_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(MessageRecord)
                    .where(MessageRecord.segment_id.is_(None))
                )
                or 0
            )
            segment_rows = (
                await session.execute(
                    select(ConversationSegmentRecord.status, func.count())
                    .group_by(ConversationSegmentRecord.status)
                    .order_by(ConversationSegmentRecord.status)
                )
            ).all()
            summary_count = int(
                await session.scalar(
                    select(func.count()).select_from(SegmentSummaryRecord)
                )
                or 0
            )
            embedding_count = int(
                await session.scalar(
                    select(func.count()).select_from(SummaryEmbeddingRecord)
                )
                or 0
            )
            personal_memory_count = int(
                await session.scalar(
                    select(func.count()).select_from(PersonalMemoryRecord)
                )
                or 0
            )
            call_rows = (
                await session.execute(
                    select(
                        PaidAiCallRecord.purpose,
                        PaidAiCallRecord.status,
                        func.count(),
                        func.coalesce(func.sum(PaidAiCallRecord.actual_cost_microusd), 0),
                        func.coalesce(func.sum(PaidAiCallRecord.input_tokens), 0),
                        func.coalesce(func.sum(PaidAiCallRecord.output_tokens), 0),
                    )
                    .where(PaidAiCallRecord.purpose.in_(("summary", "embedding")))
                    .group_by(PaidAiCallRecord.purpose, PaidAiCallRecord.status)
                    .order_by(PaidAiCallRecord.purpose, PaidAiCallRecord.status)
                )
            ).all()
        return {
            "budget": {
                "global_spent_microusd": budget.global_spent_microusd,
                "global_reserved_microusd": budget.global_reserved_microusd,
                "background_spent_microusd": budget.background_spent_microusd,
                "background_reserved_microusd": budget.background_reserved_microusd,
            },
            "message_count": message_count,
            "sensitive_message_count": sensitive_count,
            "unsegmented_message_count": unsegmented_count,
            "segment_status_counts": {
                status: int(count) for status, count in segment_rows
            },
            "background_job_status_counts": (
                await background_repository.status_counts()
            ),
            "summary_count": summary_count,
            "embedding_chunk_count": embedding_count,
            "personal_memory_count": personal_memory_count,
            "background_paid_call_totals": [
                {
                    "purpose": purpose,
                    "status": status,
                    "call_count": int(count),
                    "actual_cost_microusd": int(actual_cost),
                    "input_tokens": int(input_tokens),
                    "output_tokens": int(output_tokens),
                }
                for purpose, status, count, actual_cost, input_tokens, output_tokens
                in call_rows
            ],
        }
    finally:
        await database.dispose()


def main(argv: list[str] | None = None) -> int:
    """執行免費分析或受控正式匯入並輸出安全 JSON。"""

    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "analyze":
            report = asyncio.run(
                _run_analysis(
                    get_settings(),
                    limit_per_channel=arguments.limit_per_channel,
                    after=arguments.after,
                )
            )
        elif arguments.command == "import-history":
            report = asyncio.run(
                _run_import(
                    get_settings(),
                    limit_per_channel=arguments.limit_per_channel,
                    after=arguments.after,
                    confirmation=arguments.confirmation,
                    maximum_approved_cost_microusd=(
                        arguments.maximum_approved_cost_microusd
                    ),
                    approval_baseline_global_committed_microusd=(
                        arguments.approval_baseline_global_committed_microusd
                    ),
                )
            )
        else:
            report = asyncio.run(_run_status(get_settings()))
    except Exception as error:
        print(
            f"歷史作業失敗：{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
