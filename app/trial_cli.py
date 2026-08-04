"""階段 9 試跑的本機啟動、暫停、恢復、結束與免費報告入口。"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import timedelta

from app.config import get_settings
from app.storage.database import Database, upgrade_database
from app.storage.trial import TrialRepository, TrialStateError

TRIAL_START_CONFIRMATION = "確認啟動階段 9 七天保守試跑"
TRIAL_RESUME_CONFIRMATION = "確認恢復階段 9 試跑"
TRIAL_FINISH_CONFIRMATION = "確認結束階段 9 試跑"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="不輸出聊天內容的階段 9 試跑管理工具")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="以目前預算建立不可重設的七天試跑基準")
    start.add_argument("--confirmation", required=True)
    commands.add_parser("status", help="免費查看試跑彙總")
    commands.add_parser("report", help="免費輸出試跑評估 JSON")
    commands.add_parser("pause", help="立即暫停所有新的試跑付費預留")
    resume = commands.add_parser("resume", help="恢復尚未到期的試跑")
    resume.add_argument("--confirmation", required=True)
    finish = commands.add_parser("finish", help="結束試跑並永久停止本次新付費預留")
    finish.add_argument("--confirmation", required=True)
    return parser


async def _run(arguments: argparse.Namespace) -> dict[str, object]:
    settings = get_settings()
    await asyncio.to_thread(upgrade_database, settings.database_url)
    database = Database(settings.database_url)
    repository = TrialRepository(database.session_factory)
    try:
        if arguments.command == "start":
            if arguments.confirmation != TRIAL_START_CONFIRMATION:
                raise ValueError(f"啟動確認文字必須完全等於：{TRIAL_START_CONFIRMATION}")
            await repository.start(
                guild_ids=settings.allowed_guild_ids,
                channel_ids=settings.allowed_channel_ids,
                companion_channel_ids=settings.companion_channel_ids,
                timezone_name=settings.trial_timezone,
                duration=timedelta(days=settings.trial_duration_days),
                global_increment_limit_microusd=(
                    settings.trial_global_increment_limit_microusd
                ),
                background_increment_limit_microusd=(
                    settings.trial_background_increment_limit_microusd
                ),
                companion_daily_reply_limit=(
                    settings.trial_companion_daily_reply_limit
                ),
            )
        elif arguments.command == "pause":
            await repository.set_status("pause")
        elif arguments.command == "resume":
            if arguments.confirmation != TRIAL_RESUME_CONFIRMATION:
                raise ValueError(f"恢復確認文字必須完全等於：{TRIAL_RESUME_CONFIRMATION}")
            await repository.set_status("resume")
        elif arguments.command == "finish":
            if arguments.confirmation != TRIAL_FINISH_CONFIRMATION:
                raise ValueError(f"結束確認文字必須完全等於：{TRIAL_FINISH_CONFIRMATION}")
            await repository.set_status("finish")
        return await repository.report()
    finally:
        await database.dispose()


def main(arguments: list[str] | None = None) -> int:
    args = _parser().parse_args(arguments)
    try:
        report = asyncio.run(_run(args))
    except (TrialStateError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
