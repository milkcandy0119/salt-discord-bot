"""初始化、執行、驗證、還原及每日排程的備份命令。"""

from __future__ import annotations

import argparse
import logging
import time as time_module
from datetime import UTC, datetime
from pathlib import Path

from app.backup.service import BackupError, BackupService, ResticBackend, next_daily_run
from app.backup.settings import BackupSettings
from app.logging_config import configure_logging

LOGGER = logging.getLogger(__name__)


def _service(settings: BackupSettings) -> BackupService:
    backend = ResticBackend(
        repository=settings.restic_repository,
        password_file=settings.restic_password_file,
        binary=settings.restic_binary,
        timeout_seconds=settings.backup_command_timeout_seconds,
    )
    return BackupService(
        database_url=settings.database_url,
        backend=backend,
        keep_last=settings.backup_keep_last,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Discord 助手加密備份工具")
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("init", help="初始化空白 Restic 加密儲存庫")
    subcommands.add_parser("run", help="立即建立、驗證並輪替一份備份")
    verify = subcommands.add_parser("verify", help="隔離還原並驗證一份備份")
    verify.add_argument("--snapshot", default="latest")
    restore = subcommands.add_parser("restore", help="還原到不存在的新檔案")
    restore.add_argument("--snapshot", default="latest")
    restore.add_argument("--target", type=Path, required=True)
    subcommands.add_parser("schedule", help="依 UTC 每日時間持續執行")
    return parser


def _run_schedule(settings: BackupSettings, service: BackupService) -> None:
    """每日失敗只記錄安全原因並等待隔日，絕不因此輪替舊備份。"""

    while True:
        now = datetime.now(UTC)
        run_at = next_daily_run(now=now, daily_time=settings.daily_time)
        delay_seconds = max(1.0, (run_at - now).total_seconds())
        LOGGER.info("下次加密備份時間 UTC=%s", run_at.isoformat())
        time_module.sleep(delay_seconds)
        try:
            result = service.run_backup()
        except BackupError as error:
            LOGGER.error("每日加密備份失敗 reason=%s", error)
        else:
            LOGGER.info("每日加密備份完成 snapshot_id=%s", result.snapshot_id)


def main(arguments: list[str] | None = None) -> int:
    """執行單一命令；不輸出密碼、Discord Token 或資料內容。"""

    args = _parser().parse_args(arguments)
    settings = BackupSettings()
    configure_logging("INFO")
    service = _service(settings)
    try:
        if args.command == "init":
            service.initialize()
            LOGGER.info("Restic 加密儲存庫初始化完成")
        elif args.command == "run":
            result = service.run_backup()
            LOGGER.info("加密備份、實際還原驗證與輪替完成 snapshot_id=%s", result.snapshot_id)
        elif args.command == "verify":
            service.verify(snapshot=args.snapshot)
            LOGGER.info("備份隔離還原驗證完成 snapshot=%s", args.snapshot)
        elif args.command == "restore":
            snapshot_id = service.restore(snapshot=args.snapshot, target=args.target)
            LOGGER.info(
                "備份已還原到隔離位置 snapshot_id=%s target=%s",
                snapshot_id,
                args.target.resolve(),
            )
        elif args.command == "schedule":
            _run_schedule(settings, service)
    except BackupError as error:
        LOGGER.error("備份命令失敗 reason=%s", error)
        return 1
    except KeyboardInterrupt:
        LOGGER.info("備份排程已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
