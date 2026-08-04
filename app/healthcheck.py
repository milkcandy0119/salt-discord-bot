"""供 Docker HEALTHCHECK 呼叫的無網路命令。"""

from __future__ import annotations

import logging

from app.config import get_settings
from app.health import HealthCheckError, run_health_check
from app.logging_config import configure_logging

LOGGER = logging.getLogger(__name__)


def main() -> int:
    """成功回傳 0；失敗只輸出不含祕密的原因。"""

    settings = get_settings()
    configure_logging(settings.log_level)
    try:
        run_health_check(
            database_url=settings.database_url,
            heartbeat_path=settings.health_heartbeat_path,
            max_age_seconds=settings.health_max_age_seconds,
        )
    except HealthCheckError as error:
        LOGGER.error("容器健康檢查失敗 reason=%s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
