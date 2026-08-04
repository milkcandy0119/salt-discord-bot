"""Discord CDN 限定下載與靜態圖片安全正規化。"""

from __future__ import annotations

import asyncio
import base64
import bisect
import io
import warnings
from dataclasses import dataclass
from pathlib import PurePosixPath
from time import perf_counter
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from app.vision.models import IncomingVisual, PreparedImage, VisualMediaKind

_ALLOWED_CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
_EXPECTED_PATH_PREFIX = {
    VisualMediaKind.ATTACHMENT: "attachments",
    VisualMediaKind.STICKER: "stickers",
    VisualMediaKind.CUSTOM_EMOJI: "emojis",
}
_FORMAT_CONTENT_TYPE = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
_FORMAT_SUFFIXES = {
    "JPEG": frozenset({".jpg", ".jpeg"}),
    "PNG": frozenset({".png"}),
    "WEBP": frozenset({".webp"}),
}


class VisionInputError(RuntimeError):
    """只攜帶固定錯誤代碼，避免例外文字夾帶 CDN 網址或圖片內容。"""


class VisionDownloader(Protocol):
    """可由離線測試替換的受限下載介面。"""

    async def download(
        self,
        source_url: str,
        *,
        maximum_bytes: int,
        timeout_seconds: float,
    ) -> bytes: ...


class DiscordCdnDownloader:
    """不跟隨重新導向，並在串流讀取期間強制限制大小。"""

    async def download(
        self,
        source_url: str,
        *,
        maximum_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        chunks: list[bytes] = []
        downloaded = 0
        timeout = httpx.Timeout(timeout_seconds)
        try:
            async with (
                httpx.AsyncClient(follow_redirects=False, timeout=timeout) as client,
                client.stream("GET", source_url) as response,
            ):
                if response.status_code != 200:
                    raise VisionInputError("download_failed")
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        if int(declared_length) > maximum_bytes:
                            raise VisionInputError("download_too_large")
                    except ValueError as error:
                        raise VisionInputError("invalid_content_length") from error
                async for chunk in response.aiter_bytes():
                    downloaded += len(chunk)
                    if downloaded > maximum_bytes:
                        raise VisionInputError("download_too_large")
                    chunks.append(chunk)
        except VisionInputError:
            raise
        except (TimeoutError, httpx.HTTPError) as error:
            raise VisionInputError("download_failed") from error
        return b"".join(chunks)


@dataclass(frozen=True, slots=True)
class VisionPreparation:
    """圖片準備結果只保留固定狀態，不含來源網址或原始位元組。"""

    images: tuple[PreparedImage, ...]
    failure_codes: tuple[str, ...]
    considered_count: int
    animation_format: Literal["gif", "apng"] | None = None


class VisionService:
    """安全處理靜態圖片，或從單一 GIF／APNG 擷取代表畫面。"""

    def __init__(
        self,
        *,
        enabled: bool,
        maximum_images_per_message: int,
        maximum_download_bytes: int,
        maximum_pixels: int,
        download_timeout_seconds: float,
        detail: Literal["low", "auto"],
        maximum_dimension: int = 1_536,
        maximum_animations_per_message: int = 1,
        maximum_frames_per_animation: int = 4,
        maximum_animation_frames: int = 300,
        maximum_animation_total_pixels: int = 80_000_000,
        animation_processing_timeout_seconds: float = 3.0,
        maximum_animation_duration_seconds: float = 30.0,
        animation_duplicate_threshold: float = 3.0,
        downloader: VisionDownloader | None = None,
    ) -> None:
        if maximum_images_per_message < 1:
            raise ValueError("每則訊息圖片上限必須大於零")
        if maximum_download_bytes < 1 or maximum_pixels < 1:
            raise ValueError("圖片大小限制必須大於零")
        if download_timeout_seconds <= 0 or maximum_dimension < 1:
            raise ValueError("圖片逾時與邊長限制必須大於零")
        if maximum_animations_per_message != 1:
            raise ValueError("第二階段每則訊息只能處理一個動畫")
        if maximum_frames_per_animation < 1 or maximum_animation_frames < 1:
            raise ValueError("動畫畫面與影格上限必須大於零")
        if maximum_frames_per_animation > maximum_animation_frames:
            raise ValueError("擷取畫面上限不得大於動畫影格上限")
        if maximum_animation_total_pixels < 1:
            raise ValueError("動畫總像素上限必須大於零")
        if animation_processing_timeout_seconds <= 0:
            raise ValueError("動畫處理逾時必須大於零")
        if maximum_animation_duration_seconds <= 0:
            raise ValueError("動畫時間長度上限必須大於零")
        if not 0 <= animation_duplicate_threshold <= 255:
            raise ValueError("動畫近似畫面門檻必須介於 0 到 255")
        self.enabled = enabled
        self.maximum_images_per_message = maximum_images_per_message
        self.maximum_download_bytes = maximum_download_bytes
        self.maximum_pixels = maximum_pixels
        self.download_timeout_seconds = download_timeout_seconds
        self.detail = detail
        self.maximum_dimension = maximum_dimension
        self.maximum_animations_per_message = maximum_animations_per_message
        self.maximum_frames_per_animation = maximum_frames_per_animation
        self.maximum_animation_frames = maximum_animation_frames
        self.maximum_animation_total_pixels = maximum_animation_total_pixels
        self.animation_processing_timeout_seconds = animation_processing_timeout_seconds
        self.maximum_animation_duration_seconds = maximum_animation_duration_seconds
        self.animation_duplicate_threshold = animation_duplicate_threshold
        self._downloader = downloader or DiscordCdnDownloader()

    def maximum_model_images(self, candidates: tuple[IncomingVisual, ...]) -> int:
        """傳回下載前用於預算預留的最壞情況圖片張數。"""

        if not self.enabled:
            return 0
        if any(candidate.is_supported_animation_candidate for candidate in candidates):
            return self.maximum_frames_per_animation
        static_count = sum(candidate.is_static_candidate for candidate in candidates)
        return min(static_count, self.maximum_images_per_message)

    async def prepare(
        self,
        candidates: tuple[IncomingVisual, ...],
    ) -> VisionPreparation:
        """依事件順序處理有限數量；所有圖片資料離開此方法後只剩 data URL。"""

        if not self.enabled:
            return VisionPreparation((), ("vision_disabled",) if candidates else (), 0)

        failures: list[str] = []
        supported_animations = [
            (index, candidate)
            for index, candidate in enumerate(candidates)
            if candidate.is_supported_animation_candidate
        ]
        if supported_animations:
            # 明確標示為 GIF／APNG 的資源優先於可能是 APNG 的一般 PNG。
            _, selected = min(
                supported_animations,
                key=lambda item: (not item[1].animation_is_declared, item[0]),
            )
            failures.extend(
                "animation_count_exceeded"
                for _, candidate in supported_animations
                if candidate is not selected
            )
            failures.extend(
                "lottie_not_supported"
                for candidate in candidates
                if candidate.animation_format == "lottie"
            )
            try:
                self._validate_before_download(selected)
                raw = await self._download(selected)
                images, animation_format = await asyncio.wait_for(
                    asyncio.to_thread(self._normalize_visual, raw, selected),
                    timeout=self.animation_processing_timeout_seconds,
                )
            except TimeoutError:
                failures.append("animation_processing_timeout")
                return VisionPreparation((), tuple(failures), 1)
            except VisionInputError as error:
                failures.append(str(error))
                return VisionPreparation((), tuple(failures), 1)
            return VisionPreparation(images, tuple(failures), 1, animation_format)

        images: list[PreparedImage] = []
        considered = 0
        for candidate in candidates:
            if candidate.animation_format == "lottie":
                failures.append("lottie_not_supported")
                continue
            if not candidate.is_static_candidate:
                failures.append("animated_not_supported")
                continue
            if considered >= self.maximum_images_per_message:
                failures.append("image_count_exceeded")
                continue
            considered += 1
            try:
                self._validate_before_download(candidate)
                raw = await self._download(candidate)
                prepared, animation_format = await asyncio.to_thread(
                    self._normalize_visual,
                    raw,
                    candidate,
                )
            except TimeoutError:
                failures.append("download_timeout")
            except VisionInputError as error:
                failures.append(str(error))
            else:
                if animation_format is not None:
                    raise RuntimeError("靜態候選意外產生動畫畫面")
                images.extend(prepared)
        return VisionPreparation(tuple(images), tuple(failures), considered)

    async def _download(self, candidate: IncomingVisual) -> bytes:
        try:
            raw = await asyncio.wait_for(
                self._downloader.download(
                    candidate.source_url,
                    maximum_bytes=self.maximum_download_bytes,
                    timeout_seconds=self.download_timeout_seconds,
                ),
                timeout=self.download_timeout_seconds,
            )
        except TimeoutError:
            raise VisionInputError("download_timeout") from None
        if not raw or len(raw) > self.maximum_download_bytes:
            raise VisionInputError("download_too_large")
        return raw

    def _validate_before_download(self, candidate: IncomingVisual) -> None:
        if candidate.declared_size is not None:
            if candidate.declared_size < 0:
                raise VisionInputError("invalid_declared_size")
            if candidate.declared_size > self.maximum_download_bytes:
                raise VisionInputError("declared_too_large")
        allowed_content_types = set(_FORMAT_CONTENT_TYPE.values())
        if candidate.animation_format == "gif":
            allowed_content_types = {"image/gif"}
        elif candidate.animation_format == "apng":
            allowed_content_types = {"image/png", "image/apng"}
        if candidate.declared_content_type not in allowed_content_types:
            raise VisionInputError("unsupported_declared_type")

        parsed = urlsplit(candidate.source_url)
        try:
            port = parsed.port
        except ValueError:
            raise VisionInputError("invalid_discord_source") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _ALLOWED_CDN_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or port not in {None, 443}
        ):
            raise VisionInputError("invalid_discord_source")
        path_parts = tuple(part for part in parsed.path.split("/") if part)
        expected_prefix = _EXPECTED_PATH_PREFIX[candidate.media_kind]
        if not path_parts or path_parts[0] != expected_prefix:
            raise VisionInputError("invalid_discord_source")
        if not any(candidate.resource_id == PurePosixPath(part).stem for part in path_parts):
            raise VisionInputError("invalid_discord_source")

    def _normalize_visual(
        self,
        raw: bytes,
        candidate: IncomingVisual,
    ) -> tuple[tuple[PreparedImage, ...], Literal["gif", "apng"] | None]:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as source:
                    actual_format = (source.format or "").upper()
                    frame_count = int(getattr(source, "n_frames", 1))
                    is_animated = bool(getattr(source, "is_animated", False)) and frame_count > 1
                    if is_animated:
                        animation_format = self._validate_animation_format(
                            actual_format,
                            candidate,
                        )
                        images = self._sample_animation(
                            source,
                            candidate,
                            animation_format=animation_format,
                            frame_count=frame_count,
                        )
                        return images, animation_format
                    normalized = self._normalize_static_source(source, candidate)
        except VisionInputError:
            raise
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise VisionInputError("decompression_bomb") from None
        except (UnidentifiedImageError, OSError, OverflowError, ValueError):
            raise VisionInputError("invalid_image") from None

        return (
            (PreparedImage(data_url=self._to_data_url(normalized), detail=self.detail),),
            None,
        )

    def _normalize_static_source(
        self,
        source: Image.Image,
        candidate: IncomingVisual,
    ) -> bytes:
        actual_format = (source.format or "").upper()
        if actual_format not in _FORMAT_CONTENT_TYPE:
            raise VisionInputError("unsupported_actual_format")
        width, height = source.size
        if width < 1 or height < 1 or width * height > self.maximum_pixels:
            raise VisionInputError("pixel_limit_exceeded")
        suffix = PurePosixPath(candidate.filename).suffix.lower()
        if suffix not in _FORMAT_SUFFIXES[actual_format]:
            raise VisionInputError("format_mismatch")
        actual_content_type = _FORMAT_CONTENT_TYPE[actual_format]
        if candidate.declared_content_type not in {actual_content_type, "image/apng"}:
            raise VisionInputError("format_mismatch")
        source.load()
        return self._encode_frame(source)

    def _validate_animation_format(
        self,
        actual_format: str,
        candidate: IncomingVisual,
    ) -> Literal["gif", "apng"]:
        if actual_format == "GIF" and candidate.animation_format == "gif":
            if PurePosixPath(candidate.filename).suffix.lower() != ".gif":
                raise VisionInputError("format_mismatch")
            return "gif"
        if actual_format == "PNG" and candidate.animation_format == "apng":
            if PurePosixPath(candidate.filename).suffix.lower() not in {".png", ".apng"}:
                raise VisionInputError("format_mismatch")
            return "apng"
        raise VisionInputError("format_mismatch")

    def _sample_animation(
        self,
        source: Image.Image,
        candidate: IncomingVisual,
        *,
        animation_format: Literal["gif", "apng"],
        frame_count: int,
    ) -> tuple[PreparedImage, ...]:
        del candidate
        deadline = perf_counter() + self.animation_processing_timeout_seconds
        width, height = source.size
        if width < 1 or height < 1 or width * height > self.maximum_pixels:
            raise VisionInputError("pixel_limit_exceeded")
        if frame_count > self.maximum_animation_frames:
            raise VisionInputError("animation_frame_limit_exceeded")
        if width * height * frame_count > self.maximum_animation_total_pixels:
            raise VisionInputError("animation_total_pixels_exceeded")

        durations: list[int] = []
        cumulative_ends: list[int] = []
        total_duration_ms = 0
        for frame_index in range(frame_count):
            self._check_processing_deadline(deadline)
            source.seek(frame_index)
            raw_duration = source.info.get("duration", 100)
            try:
                duration_ms = max(int(raw_duration), 1)
            except (OverflowError, TypeError, ValueError):
                duration_ms = 100
            total_duration_ms += duration_ms
            if total_duration_ms > self.maximum_animation_duration_seconds * 1_000:
                raise VisionInputError("animation_duration_exceeded")
            durations.append(duration_ms)
            cumulative_ends.append(total_duration_ms)

        pool_size = min(frame_count, self.maximum_frames_per_animation * 4)
        if pool_size == 1:
            target_times = [0]
        else:
            target_times = [
                round(index * max(total_duration_ms - 1, 0) / (pool_size - 1))
                for index in range(pool_size)
            ]
        candidate_frames: list[tuple[int, int]] = []
        seen_indexes: set[int] = set()
        for target_time in target_times:
            frame_index = min(
                bisect.bisect_right(cumulative_ends, target_time),
                frame_count - 1,
            )
            if frame_index in seen_indexes:
                continue
            seen_indexes.add(frame_index)
            timestamp_ms = cumulative_ends[frame_index] - durations[frame_index]
            candidate_frames.append((frame_index, timestamp_ms))

        selected: list[tuple[bytes, bytes, int]] = []
        for frame_index, timestamp_ms in candidate_frames:
            self._check_processing_deadline(deadline)
            source.seek(frame_index)
            source.load()
            rgb = source.convert("RGB")
            try:
                fingerprint = self._frame_fingerprint(rgb)
                if any(
                    self._mean_absolute_difference(fingerprint, prior) <= (
                        self.animation_duplicate_threshold
                    )
                    for _, prior, _ in selected
                ):
                    continue
                normalized = self._encode_frame(rgb)
            finally:
                rgb.close()
            selected.append((normalized, fingerprint, timestamp_ms))
            if len(selected) >= self.maximum_frames_per_animation:
                break

        if not selected:
            raise VisionInputError("animation_has_no_usable_frames")
        total = len(selected)
        return tuple(
            PreparedImage(
                data_url=self._to_data_url(normalized),
                detail=self.detail,
                sequence_index=index,
                sequence_total=total,
                timestamp_ms=timestamp_ms,
                animation_format=animation_format,
            )
            for index, (normalized, _, timestamp_ms) in enumerate(selected, start=1)
        )

    def _encode_frame(self, image: Image.Image) -> bytes:
        transposed = ImageOps.exif_transpose(image)
        try:
            rgb = transposed.convert("RGB")
            rgb.thumbnail(
                (self.maximum_dimension, self.maximum_dimension),
                Image.Resampling.LANCZOS,
            )
            output = io.BytesIO()
            try:
                rgb.save(output, format="JPEG", quality=85, optimize=False)
                return output.getvalue()
            finally:
                output.close()
                rgb.close()
        finally:
            if transposed is not image:
                transposed.close()

    @staticmethod
    def _frame_fingerprint(image: Image.Image) -> bytes:
        thumbnail = image.resize((16, 16), Image.Resampling.BILINEAR).convert("RGB")
        try:
            return thumbnail.tobytes()
        finally:
            thumbnail.close()

    @staticmethod
    def _mean_absolute_difference(left: bytes, right: bytes) -> float:
        if len(left) != len(right) or not left:
            return 255.0
        return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / len(left)

    @staticmethod
    def _to_data_url(normalized: bytes) -> str:
        encoded = base64.b64encode(normalized).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"

    @staticmethod
    def _check_processing_deadline(deadline: float) -> None:
        if perf_counter() > deadline:
            raise VisionInputError("animation_processing_timeout")
