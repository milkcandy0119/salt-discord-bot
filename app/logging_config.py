"""最小化的應用程式日誌設定。"""

from __future__ import annotations

import logging


def configure_logging(level: str) -> None:
    """設定簡潔的日誌格式，避免輸出完整應用程式設定。"""

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
