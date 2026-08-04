"""不持久化圖片位元組的視覺輸入資料模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

AnimationFormat = Literal["gif", "apng", "lottie"]


class VisualMediaKind(StrEnum):
    """可辨識的 Discord 視覺資源來源。"""

    ATTACHMENT = "attachment"
    STICKER = "sticker"
    CUSTOM_EMOJI = "custom_emoji"


@dataclass(frozen=True, slots=True)
class IncomingVisual:
    """Discord 事件當下取得的視覺 metadata；來源網址不得保存或記錄。"""

    resource_id: str
    media_kind: VisualMediaKind
    filename: str
    declared_content_type: str | None
    declared_size: int | None
    source_url: str = field(repr=False)
    possibly_animated: bool = False
    animation_format: AnimationFormat | None = None
    animation_is_declared: bool = False
    display_name: str | None = None

    @property
    def is_static_candidate(self) -> bool:
        """只將有機會是靜態圖片的資源交給後續安全檢查。"""

        return not self.possibly_animated

    @property
    def is_supported_animation_candidate(self) -> bool:
        """GIF 與 APNG 可交由第二階段本機取樣；Lottie 維持名稱-only。"""

        return self.animation_format in {"gif", "apng"}

    @property
    def is_processable_candidate(self) -> bool:
        """指出此資源是否有可能產生一張或多張模型圖片。"""

        return self.is_static_candidate or self.is_supported_animation_candidate


@dataclass(frozen=True, slots=True)
class PreparedImage:
    """只在單次 API 呼叫期間存在的正規化圖片。"""

    data_url: str = field(repr=False)
    detail: Literal["low", "auto"]
    sequence_index: int | None = None
    sequence_total: int | None = None
    timestamp_ms: int | None = None
    animation_format: Literal["gif", "apng"] | None = None
