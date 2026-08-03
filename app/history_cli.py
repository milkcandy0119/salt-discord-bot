"""階段 6 歷史訊息免費分析命令列入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta

from app.ai.budget_manager import BudgetManager, ModelPrice
from app.config import Settings, get_settings
from app.history.analyzer import HistoryAnalyzer
from app.history.discord_source import DiscordHistorySource
from app.security.sensitive_filter import SensitiveFilter
from app.storage.database import Database


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


def main(argv: list[str] | None = None) -> int:
    """執行免費分析並將安全 JSON 寫到標準輸出。"""

    arguments = _build_parser().parse_args(argv)
    try:
        report = asyncio.run(
            _run_analysis(
                get_settings(),
                limit_per_channel=arguments.limit_per_channel,
                after=arguments.after,
            )
        )
    except Exception as error:
        print(
            f"歷史分析失敗：{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
