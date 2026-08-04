"""只從目前 Discord 訊息事件建立視覺候選，不接受一般外部網址。"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

import discord

from app.vision.models import IncomingVisual, VisualMediaKind

_CUSTOM_EMOJI_PATTERN = re.compile(r"<(a?):([A-Za-z0-9_]{2,32}):(\d+)>")
_STATIC_IMAGE_TYPES = {"image/jpeg", "image/webp"}
_STATIC_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def extract_discord_visuals(message: discord.Message) -> tuple[IncomingVisual, ...]:
    """擷取目前事件提供的附件、貼圖與自訂表情來源。"""

    visuals: list[IncomingVisual] = []
    for attachment in getattr(message, "attachments", ()):
        content_type = _normalize_content_type(getattr(attachment, "content_type", None))
        filename = str(getattr(attachment, "filename", "attachment"))
        suffix = PurePosixPath(filename).suffix.lower()
        animation_format = None
        animation_is_declared = False
        if content_type == "image/gif" or suffix == ".gif":
            animation_format = "gif"
            animation_is_declared = True
        elif content_type == "image/apng" or suffix == ".apng":
            animation_format = "apng"
            animation_is_declared = True
        elif content_type == "image/png" or suffix == ".png":
            # 一般 PNG 與 APNG 可能具有相同副檔名及 Content-Type，下載後再以 Pillow 確認。
            animation_format = "apng"
        image_like = content_type is not None and content_type.startswith("image/")
        image_like = image_like or suffix in _STATIC_IMAGE_SUFFIXES or suffix in {".gif", ".apng"}
        if not image_like:
            continue
        visuals.append(
            IncomingVisual(
                resource_id=str(attachment.id),
                media_kind=VisualMediaKind.ATTACHMENT,
                filename=filename,
                declared_content_type=content_type,
                declared_size=getattr(attachment, "size", None),
                source_url=str(attachment.url),
                possibly_animated=(
                    animation_format is not None
                    or content_type not in _STATIC_IMAGE_TYPES
                ),
                animation_format=animation_format,
                animation_is_declared=animation_is_declared,
            )
        )

    for sticker in getattr(message, "stickers", ()):
        name = str(getattr(sticker, "name", "sticker"))
        sticker_format = getattr(sticker, "format", None)
        is_static = sticker_format is discord.StickerFormatType.png
        sticker_animation_format = {
            discord.StickerFormatType.apng: "apng",
            discord.StickerFormatType.gif: "gif",
            discord.StickerFormatType.lottie: "lottie",
        }.get(sticker_format)
        extension = getattr(sticker_format, "file_extension", "unknown")
        content_type = {
            discord.StickerFormatType.png: "image/png",
            discord.StickerFormatType.apng: "image/png",
            discord.StickerFormatType.gif: "image/gif",
            discord.StickerFormatType.lottie: "application/json",
        }.get(sticker_format)
        visuals.append(
            IncomingVisual(
                resource_id=str(sticker.id),
                media_kind=VisualMediaKind.STICKER,
                filename=f"{name}.{extension}",
                declared_content_type=content_type,
                declared_size=None,
                source_url=str(sticker.url),
                possibly_animated=not is_static,
                animation_format=sticker_animation_format,
                animation_is_declared=not is_static,
                display_name=name,
            )
        )

    seen_emoji_ids: set[str] = set()
    for match in _CUSTOM_EMOJI_PATTERN.finditer(message.content):
        animated_marker, name, emoji_id = match.groups()
        if emoji_id in seen_emoji_ids:
            continue
        seen_emoji_ids.add(emoji_id)
        animated = bool(animated_marker)
        extension = "gif" if animated else "png"
        visuals.append(
            IncomingVisual(
                resource_id=emoji_id,
                media_kind=VisualMediaKind.CUSTOM_EMOJI,
                filename=f"{name}.{extension}",
                declared_content_type="image/gif" if animated else "image/png",
                declared_size=None,
                source_url=f"https://cdn.discordapp.com/emojis/{emoji_id}.{extension}",
                possibly_animated=animated,
                animation_format="gif" if animated else None,
                animation_is_declared=animated,
                display_name=name,
            )
        )
    return tuple(visuals)


def _normalize_content_type(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.partition(";")[0].strip().lower()
    return normalized or None
