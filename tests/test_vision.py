from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

from app.ai.budget_manager import BudgetManager, ModelPrice, PaidPurpose
from app.ai.chat_service import (
    VISION_UNAVAILABLE_MESSAGE,
    ChatService,
    OpenAIResponsesProvider,
    ProviderChatResponse,
)
from app.ai.persona import Persona
from app.bot.channel_modes import ChannelMode, ReplySignals, ReplyTriggerPolicy
from app.bot.message_handler import IncomingMessage, MessageHandler, compose_stored_content
from app.conversations.context_builder import ChatContext, ProviderInputMessage
from app.security.sensitive_filter import SensitiveFilter
from app.storage.database import Database
from app.storage.repositories import MessageRepository
from app.vision.discord_sources import extract_discord_visuals
from app.vision.models import IncomingVisual, PreparedImage, VisualMediaKind
from app.vision.service import VisionService


def make_image_bytes(
    image_format: str = "PNG",
    *,
    size: tuple[int, int] = (8, 6),
    exif_description: str | None = None,
) -> bytes:
    """建立不依賴網路的小型圖片 fixture。"""

    image = Image.new("RGBA" if image_format == "PNG" else "RGB", size, "#6d8fb3")
    output = io.BytesIO()
    options: dict[str, object] = {}
    if exif_description is not None:
        exif = Image.Exif()
        exif[0x010E] = exif_description
        options["exif"] = exif
    image.save(output, format=image_format, **options)
    image.close()
    return output.getvalue()


def make_animated_gif_bytes() -> bytes:
    first = Image.new("RGB", (4, 4), "red")
    second = Image.new("RGB", (4, 4), "blue")
    output = io.BytesIO()
    first.save(output, format="GIF", save_all=True, append_images=[second], duration=50, loop=0)
    first.close()
    second.close()
    return output.getvalue()


def make_animation_bytes(
    image_format: str,
    colors: tuple[str, ...],
    *,
    size: tuple[int, int] = (8, 6),
    duration_ms: int = 100,
) -> bytes:
    frames = [Image.new("RGBA", size, color) for color in colors]
    output = io.BytesIO()
    options: dict[str, object] = {
        "save_all": True,
        "append_images": frames[1:],
        "duration": [duration_ms] * len(frames),
        "loop": 0,
    }
    if image_format == "GIF":
        options["disposal"] = 2
        options["optimize"] = False
    else:
        options["disposal"] = [0] * len(frames)
        options["blend"] = [0] * len(frames)
    frames[0].save(output, format=image_format, **options)
    for frame in frames:
        frame.close()
    return output.getvalue()


def make_visual(
    *,
    resource_id: str = "123",
    kind: VisualMediaKind = VisualMediaKind.ATTACHMENT,
    filename: str = "image.png",
    content_type: str | None = "image/png",
    declared_size: int | None = 100,
    source_url: str | None = None,
    animated: bool = False,
    animation_format: str | None = None,
    animation_is_declared: bool = False,
    display_name: str | None = None,
) -> IncomingVisual:
    prefixes = {
        VisualMediaKind.ATTACHMENT: f"attachments/1/{resource_id}",
        VisualMediaKind.STICKER: "stickers",
        VisualMediaKind.CUSTOM_EMOJI: "emojis",
    }
    return IncomingVisual(
        resource_id=resource_id,
        media_kind=kind,
        filename=filename,
        declared_content_type=content_type,
        declared_size=declared_size,
        source_url=source_url
        or f"https://cdn.discordapp.com/{prefixes[kind]}/{filename}",
        possibly_animated=animated,
        animation_format=animation_format,  # type: ignore[arg-type]
        animation_is_declared=animation_is_declared,
        display_name=display_name,
    )


@dataclass
class RecordingDownloader:
    payload: bytes
    calls: list[str] = field(default_factory=list)

    async def download(
        self,
        source_url: str,
        *,
        maximum_bytes: int,
        timeout_seconds: float,
    ) -> bytes:
        del maximum_bytes, timeout_seconds
        self.calls.append(source_url)
        return self.payload


def make_vision_service(
    downloader: RecordingDownloader,
    *,
    enabled: bool = True,
    maximum_images: int = 1,
    maximum_bytes: int = 16_384,
    maximum_pixels: int = 1_000,
    maximum_dimension: int = 64,
    maximum_frames_per_animation: int = 4,
    maximum_animation_frames: int = 300,
    maximum_animation_total_pixels: int = 80_000_000,
    animation_processing_timeout_seconds: float = 1,
    maximum_animation_duration_seconds: float = 30,
    animation_duplicate_threshold: float = 3,
) -> VisionService:
    return VisionService(
        enabled=enabled,
        maximum_images_per_message=maximum_images,
        maximum_download_bytes=maximum_bytes,
        maximum_pixels=maximum_pixels,
        download_timeout_seconds=1,
        detail="low",
        maximum_dimension=maximum_dimension,
        maximum_animations_per_message=1,
        maximum_frames_per_animation=maximum_frames_per_animation,
        maximum_animation_frames=maximum_animation_frames,
        maximum_animation_total_pixels=maximum_animation_total_pixels,
        animation_processing_timeout_seconds=animation_processing_timeout_seconds,
        maximum_animation_duration_seconds=maximum_animation_duration_seconds,
        animation_duplicate_threshold=animation_duplicate_threshold,
        downloader=downloader,
    )


def make_context(content: str = "[Discord 視覺資源：kind=attachment]") -> ChatContext:
    message = ProviderInputMessage("user", content, "100")
    return ChatContext("100", (message,), len(content))


@dataclass
class RecordingProvider:
    calls: list[dict[str, object]] = field(default_factory=list)

    async def generate(self, **arguments: object) -> ProviderChatResponse:
        self.calls.append(arguments)
        return ProviderChatResponse("resp_vision", "看起來很好吃。", 100, 20)


def make_chat_service(
    database: Database,
    provider: RecordingProvider,
    vision_service: VisionService,
) -> ChatService:
    return ChatService(
        provider=provider,
        budget_manager=BudgetManager(database.session_factory),
        price=ModelPrice("gpt-5.6-luna", "test", 1_000_000, 6_000_000),
        persona=Persona("salt", "v-test", "Salt", "使用自然的繁體中文。"),
        sensitive_filter=SensitiveFilter(),
        maintenance_message="目前暫時無法使用。",
        maximum_output_tokens=100,
        reasoning_effort="low",
        vision_service=vision_service,
        maximum_reserved_tokens_per_image=1_200,
    )


def test_unicode_emoji_remains_text_without_visual_candidate() -> None:
    message = SimpleNamespace(content="今天好累 💤", attachments=(), stickers=())

    assert extract_discord_visuals(message) == ()


@pytest.mark.asyncio
async def test_static_png_is_normalized_to_rgb_jpeg_and_exif_is_removed() -> None:
    payload = make_image_bytes("PNG", size=(80, 40))
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader, maximum_pixels=10_000, maximum_dimension=32)

    result = await service.prepare((make_visual(declared_size=len(payload)),))

    assert result.failure_codes == ()
    assert len(result.images) == 1
    encoded = result.images[0].data_url.removeprefix("data:image/jpeg;base64,")
    normalized = base64.b64decode(encoded)
    with Image.open(io.BytesIO(normalized)) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert image.size == (32, 16)
        assert not image.getexif()


def test_only_trigger_message_receives_prepared_images() -> None:
    image = PreparedImage("data:image/jpeg;base64,ZmFrZQ==", "low")
    context = ChatContext(
        trigger_message_id="2",
        messages=(
            ProviderInputMessage("user", "歷史文字", "1"),
            ProviderInputMessage("user", "目前訊息", "2"),
        ),
        character_count=8,
    )

    updated = context.with_trigger_images((image,))

    assert updated.messages[0].images == ()
    assert updated.messages[1].images == (image,)


@pytest.mark.asyncio
async def test_declared_oversized_image_is_rejected_before_download() -> None:
    downloader = RecordingDownloader(make_image_bytes())
    service = make_vision_service(downloader, maximum_bytes=100)

    result = await service.prepare((make_visual(declared_size=101),))

    assert result.failure_codes == ("declared_too_large",)
    assert downloader.calls == []


@pytest.mark.asyncio
async def test_filename_or_content_type_mismatch_is_rejected() -> None:
    payload = make_image_bytes("PNG")
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader)

    result = await service.prepare(
        (make_visual(filename="not-really-jpeg.jpg", declared_size=len(payload)),)
    )

    assert result.images == ()
    assert result.failure_codes == ("format_mismatch",)


@pytest.mark.asyncio
async def test_pixel_limit_blocks_image_before_full_decode() -> None:
    payload = make_image_bytes("PNG", size=(20, 20))
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader, maximum_pixels=399)

    result = await service.prepare((make_visual(declared_size=len(payload)),))

    assert result.failure_codes == ("pixel_limit_exceeded",)


@pytest.mark.asyncio
async def test_animated_gif_never_becomes_image_input() -> None:
    payload = make_animated_gif_bytes()
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader)
    declared_animated = make_visual(
        filename="dance.gif",
        content_type="image/gif",
        declared_size=len(payload),
        animated=True,
    )

    result = await service.prepare((declared_animated,))

    assert result.images == ()
    assert result.failure_codes == ("animated_not_supported",)
    assert downloader.calls == []


@pytest.mark.asyncio
async def test_dynamic_gif_yields_at_most_four_ordered_representative_frames() -> None:
    payload = make_animation_bytes(
        "GIF",
        ("red", "lime", "blue", "yellow", "purple", "white"),
    )
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader, maximum_frames_per_animation=4)
    visual = make_visual(
        filename="story.gif",
        content_type="image/gif",
        declared_size=len(payload),
        animated=True,
        animation_format="gif",
        animation_is_declared=True,
    )

    result = await service.prepare((visual,))

    assert result.animation_format == "gif"
    assert len(result.images) == 4
    assert [image.sequence_index for image in result.images] == [1, 2, 3, 4]
    assert all(image.sequence_total == 4 for image in result.images)
    timestamps = [image.timestamp_ms for image in result.images]
    assert timestamps == sorted(timestamps)  # type: ignore[arg-type]
    assert len({image.data_url for image in result.images}) == 4


@pytest.mark.asyncio
async def test_apng_uses_same_ordered_sampling_pipeline() -> None:
    payload = make_animation_bytes("PNG", ("red", "lime", "blue", "white"))
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader)
    visual = make_visual(
        filename="motion.png",
        content_type="image/png",
        declared_size=len(payload),
        animated=True,
        animation_format="apng",
        animation_is_declared=False,
    )

    result = await service.prepare((visual,))

    assert result.animation_format == "apng"
    assert len(result.images) == 4
    assert [image.animation_format for image in result.images] == ["apng"] * 4
    assert [image.timestamp_ms for image in result.images] == [0, 100, 200, 300]


@pytest.mark.asyncio
async def test_ordinary_png_attachment_falls_back_to_one_static_image() -> None:
    payload = make_image_bytes("PNG")
    attachment = SimpleNamespace(
        id=123,
        filename="still.png",
        content_type="image/png",
        size=len(payload),
        url="https://cdn.discordapp.com/attachments/1/123/still.png",
    )
    message = SimpleNamespace(content="", attachments=(attachment,), stickers=())
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader)

    (visual,) = extract_discord_visuals(message)
    result = await service.prepare((visual,))

    assert visual.animation_format == "apng"
    assert visual.animation_is_declared is False
    assert len(result.images) == 1
    assert result.animation_format is None
    assert result.images[0].sequence_index is None


@pytest.mark.asyncio
async def test_nearly_identical_animation_frames_are_deduplicated() -> None:
    payload = make_animation_bytes(
        "PNG",
        ("#646464", "#656565", "#666666", "#ff0000"),
    )
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader, animation_duplicate_threshold=3)
    visual = make_visual(
        filename="subtle.apng",
        content_type="image/apng",
        declared_size=len(payload),
        animated=True,
        animation_format="apng",
        animation_is_declared=True,
    )

    result = await service.prepare((visual,))

    assert result.animation_format == "apng"
    assert len(result.images) == 2
    assert [image.sequence_index for image in result.images] == [1, 2]
    assert all(image.sequence_total == 2 for image in result.images)


@pytest.mark.asyncio
async def test_only_one_animation_is_downloaded_per_message() -> None:
    payload = make_animation_bytes("GIF", ("red", "blue"))
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader)
    first = make_visual(
        resource_id="123",
        filename="one.gif",
        content_type="image/gif",
        declared_size=len(payload),
        animated=True,
        animation_format="gif",
        animation_is_declared=True,
    )
    second = make_visual(
        resource_id="124",
        filename="two.gif",
        content_type="image/gif",
        declared_size=len(payload),
        animated=True,
        animation_format="gif",
        animation_is_declared=True,
    )

    result = await service.prepare((first, second))

    assert len(downloader.calls) == 1
    assert "animation_count_exceeded" in result.failure_codes


@pytest.mark.asyncio
async def test_animation_frame_count_limit_is_enforced() -> None:
    payload = make_animation_bytes("GIF", ("red", "lime", "blue"))
    downloader = RecordingDownloader(payload)
    service = make_vision_service(
        downloader,
        maximum_frames_per_animation=2,
        maximum_animation_frames=2,
    )
    visual = make_visual(
        filename="too-many.gif",
        content_type="image/gif",
        declared_size=len(payload),
        animated=True,
        animation_format="gif",
        animation_is_declared=True,
    )

    result = await service.prepare((visual,))

    assert result.images == ()
    assert result.failure_codes == ("animation_frame_limit_exceeded",)


@pytest.mark.asyncio
async def test_animation_total_pixel_limit_is_enforced() -> None:
    payload = make_animation_bytes("PNG", ("red", "lime", "blue"), size=(10, 10))
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader, maximum_animation_total_pixels=299)
    visual = make_visual(
        filename="too-large.apng",
        content_type="image/apng",
        declared_size=len(payload),
        animated=True,
        animation_format="apng",
        animation_is_declared=True,
    )

    result = await service.prepare((visual,))

    assert result.images == ()
    assert result.failure_codes == ("animation_total_pixels_exceeded",)


@pytest.mark.asyncio
async def test_animation_duration_limit_is_enforced() -> None:
    payload = make_animation_bytes(
        "GIF",
        ("red", "lime", "blue"),
        duration_ms=500,
    )
    downloader = RecordingDownloader(payload)
    service = make_vision_service(
        downloader,
        maximum_animation_duration_seconds=1,
    )
    visual = make_visual(
        filename="too-long.gif",
        content_type="image/gif",
        declared_size=len(payload),
        animated=True,
        animation_format="gif",
        animation_is_declared=True,
    )

    result = await service.prepare((visual,))

    assert result.images == ()
    assert result.failure_codes == ("animation_duration_exceeded",)


@pytest.mark.asyncio
async def test_animation_processing_timeout_returns_safe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = make_animation_bytes("GIF", ("red", "blue"))
    downloader = RecordingDownloader(payload)
    service = make_vision_service(
        downloader,
        animation_processing_timeout_seconds=0.01,
    )
    visual = make_visual(
        filename="slow.gif",
        content_type="image/gif",
        declared_size=len(payload),
        animated=True,
        animation_format="gif",
        animation_is_declared=True,
    )

    def slow_normalize(raw: bytes, candidate: IncomingVisual) -> object:
        del raw, candidate
        time.sleep(0.05)
        return (), "gif"

    monkeypatch.setattr(service, "_normalize_visual", slow_normalize)
    result = await service.prepare((visual,))

    assert result.images == ()
    assert result.failure_codes == ("animation_processing_timeout",)


def test_animated_custom_emoji_preserves_name_but_is_not_static_candidate() -> None:
    message = SimpleNamespace(
        content="<a:dancecat:789>",
        attachments=(),
        stickers=(),
    )

    (visual,) = extract_discord_visuals(message)

    assert visual.media_kind is VisualMediaKind.CUSTOM_EMOJI
    assert visual.display_name == "dancecat"
    assert visual.possibly_animated is True
    assert visual.is_static_candidate is False


@pytest.mark.asyncio
async def test_static_custom_emoji_is_sent_as_image_candidate() -> None:
    message = SimpleNamespace(
        content="<:breadcat:789>",
        attachments=(),
        stickers=(),
    )
    payload = make_image_bytes("PNG")
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader)

    (visual,) = extract_discord_visuals(message)
    result = await service.prepare((visual,))

    assert visual.display_name == "breadcat"
    assert visual.is_static_candidate is True
    assert len(result.images) == 1


@pytest.mark.asyncio
async def test_static_sticker_can_be_prepared_as_image() -> None:
    sticker = SimpleNamespace(
        id=456,
        name="麵包",
        format=discord.StickerFormatType.png,
        url="https://cdn.discordapp.com/stickers/456.png",
    )
    message = SimpleNamespace(content="", attachments=(), stickers=(sticker,))
    payload = make_image_bytes("PNG")
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader)

    result = await service.prepare(extract_discord_visuals(message))

    assert len(result.images) == 1
    assert result.failure_codes == ()


@pytest.mark.parametrize(
    ("sticker_format", "expected_animation", "expected_content_type"),
    [
        (discord.StickerFormatType.gif, "gif", "image/gif"),
        (discord.StickerFormatType.apng, "apng", "image/png"),
    ],
)
def test_dynamic_discord_sticker_is_classified_for_local_sampling(
    sticker_format: discord.StickerFormatType,
    expected_animation: str,
    expected_content_type: str,
) -> None:
    extension = sticker_format.file_extension
    sticker = SimpleNamespace(
        id=456,
        name="動態麵包",
        format=sticker_format,
        url=f"https://cdn.discordapp.com/stickers/456.{extension}",
    )
    message = SimpleNamespace(content="", attachments=(), stickers=(sticker,))

    (visual,) = extract_discord_visuals(message)

    assert visual.animation_format == expected_animation
    assert visual.animation_is_declared is True
    assert visual.declared_content_type == expected_content_type
    assert visual.display_name == "動態麵包"


@pytest.mark.asyncio
async def test_dynamic_gif_sticker_is_sampled_without_extra_renderer() -> None:
    sticker = SimpleNamespace(
        id=456,
        name="動態麵包",
        format=discord.StickerFormatType.gif,
        url="https://cdn.discordapp.com/stickers/456.gif",
    )
    message = SimpleNamespace(content="", attachments=(), stickers=(sticker,))
    payload = make_animation_bytes("GIF", ("red", "lime", "blue"))
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader)

    result = await service.prepare(extract_discord_visuals(message))

    assert result.animation_format == "gif"
    assert len(result.images) == 3


@pytest.mark.asyncio
async def test_lottie_sticker_remains_name_only_without_download() -> None:
    sticker = SimpleNamespace(
        id=456,
        name="向量麵包",
        format=discord.StickerFormatType.lottie,
        url="https://cdn.discordapp.com/stickers/456.json",
    )
    message = SimpleNamespace(content="", attachments=(), stickers=(sticker,))
    downloader = RecordingDownloader(b'{"v":"5.5.7"}')
    service = make_vision_service(downloader)

    (visual,) = extract_discord_visuals(message)
    result = await service.prepare((visual,))

    assert visual.display_name == "向量麵包"
    assert visual.animation_format == "lottie"
    assert result.images == ()
    assert result.failure_codes == ("lottie_not_supported",)
    assert downloader.calls == []
    stored_content = compose_stored_content("", ("向量麵包",), (visual,))
    assert "Discord 貼圖名稱：向量麵包" in stored_content
    assert "animation_format=lottie" in stored_content
    assert visual.source_url not in stored_content


@pytest.mark.asyncio
async def test_untrusted_external_url_is_rejected_without_download() -> None:
    downloader = RecordingDownloader(make_image_bytes())
    service = make_vision_service(downloader)

    result = await service.prepare(
        (make_visual(source_url="https://example.com/attachments/1/123/image.png"),)
    )

    assert result.failure_codes == ("invalid_discord_source",)
    assert downloader.calls == []


@pytest.mark.asyncio
async def test_image_count_limit_downloads_only_allowed_amount() -> None:
    payload = make_image_bytes("PNG")
    downloader = RecordingDownloader(payload)
    service = make_vision_service(downloader, maximum_images=1)
    first = make_visual(resource_id="123", declared_size=len(payload))
    second = make_visual(resource_id="124", declared_size=len(payload))

    result = await service.prepare((first, second))

    assert len(result.images) == 1
    assert result.failure_codes == ("image_count_exceeded",)
    assert len(downloader.calls) == 1


@pytest.mark.asyncio
async def test_disabled_vision_performs_no_download() -> None:
    downloader = RecordingDownloader(make_image_bytes())
    service = make_vision_service(downloader, enabled=False)

    result = await service.prepare((make_visual(),))

    assert result.images == ()
    assert result.failure_codes == ("vision_disabled",)
    assert downloader.calls == []


@pytest.mark.asyncio
async def test_budget_exhaustion_prevents_download_and_provider_call(database: Database) -> None:
    budget = BudgetManager(database.session_factory)
    await budget.reserve(
        purpose=PaidPurpose.FOREGROUND_CHAT,
        price=ModelPrice("fill", "test", 1_000_000, 0),
        maximum_input_tokens=10_000_000,
        maximum_output_tokens=0,
    )
    downloader = RecordingDownloader(make_image_bytes())
    provider = RecordingProvider()
    service = make_chat_service(database, provider, make_vision_service(downloader))

    outcome = await service.generate(
        make_context(),
        visual_inputs=(make_visual(),),
        trigger_has_text=False,
    )

    assert outcome.status == "budget_exhausted"
    assert downloader.calls == []
    assert provider.calls == []


@pytest.mark.asyncio
async def test_invalid_image_returns_fixed_reply_without_provider_call(
    database: Database,
) -> None:
    downloader = RecordingDownloader(b"not an image")
    provider = RecordingProvider()
    service = make_chat_service(database, provider, make_vision_service(downloader))

    outcome = await service.generate(
        make_context(),
        visual_inputs=(make_visual(declared_size=12),),
        trigger_has_text=False,
    )
    call = await BudgetManager(database.session_factory).get_call(outcome.reservation_id or "")

    assert outcome.status == "vision_unavailable"
    assert outcome.content == VISION_UNAVAILABLE_MESSAGE
    assert provider.calls == []
    assert call is not None and call.status == "released_unbilled"


@pytest.mark.asyncio
async def test_failed_image_can_continue_as_text_only_without_image_input(
    database: Database,
) -> None:
    downloader = RecordingDownloader(b"not an image")
    provider = RecordingProvider()
    service = make_chat_service(database, provider, make_vision_service(downloader))

    outcome = await service.generate(
        make_context("請回答文字問題"),
        visual_inputs=(make_visual(declared_size=12),),
        trigger_has_text=True,
    )

    assert outcome.status == "generated"
    assert len(provider.calls) == 1
    messages = provider.calls[0]["messages"]
    assert all(not message.images for message in messages)  # type: ignore[union-attr]


@dataclass
class RecordingBudget:
    reservations: list[dict[str, object]] = field(default_factory=list)

    async def reserve(self, **arguments: object) -> object:
        self.reservations.append(arguments)
        return SimpleNamespace(reservation_id="reserve-1")

    async def settle(self, reservation_id: str, **usage: int) -> None:
        del reservation_id, usage


@pytest.mark.asyncio
async def test_vision_reserves_extra_tokens_in_foreground_budget() -> None:
    payload = make_image_bytes("PNG")
    downloader = RecordingDownloader(payload)
    budget = RecordingBudget()
    provider = RecordingProvider()
    service = ChatService(
        provider=provider,
        budget_manager=budget,  # type: ignore[arg-type]
        price=ModelPrice("gpt-5.6-luna", "test", 1_000_000, 6_000_000),
        persona=Persona("salt", "v-test", "Salt", "自然回覆。"),
        sensitive_filter=SensitiveFilter(),
        maintenance_message="目前暫時無法使用。",
        maximum_output_tokens=100,
        reasoning_effort="low",
        vision_service=make_vision_service(downloader),
        maximum_reserved_tokens_per_image=1_200,
    )

    await service.generate(
        make_context("你看"),
        visual_inputs=(make_visual(declared_size=len(payload)),),
        trigger_has_text=True,
    )

    assert budget.reservations[0]["purpose"] is PaidPurpose.FOREGROUND_CHAT
    reserved_input = budget.reservations[0]["maximum_input_tokens"]
    assert isinstance(reserved_input, int)
    assert reserved_input >= 1_200
    sent_messages = provider.calls[0]["messages"]
    assert len(sent_messages[-1].images) == 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_animation_reserves_four_frame_tokens_before_download() -> None:
    payload = make_animation_bytes("GIF", ("red", "lime", "blue", "white"))
    downloader = RecordingDownloader(payload)
    budget = RecordingBudget()
    provider = RecordingProvider()
    service = ChatService(
        provider=provider,
        budget_manager=budget,  # type: ignore[arg-type]
        price=ModelPrice("gpt-5.6-luna", "test", 1_000_000, 6_000_000),
        persona=Persona("salt", "v-test", "Salt", "自然回覆。"),
        sensitive_filter=SensitiveFilter(),
        maintenance_message="目前暫時無法使用。",
        maximum_output_tokens=100,
        reasoning_effort="low",
        vision_service=make_vision_service(downloader, maximum_frames_per_animation=4),
        maximum_reserved_tokens_per_image=10_000,
    )
    visual = make_visual(
        filename="story.gif",
        content_type="image/gif",
        declared_size=len(payload),
        animated=True,
        animation_format="gif",
        animation_is_declared=True,
    )

    await service.generate(
        make_context("你看"),
        visual_inputs=(visual,),
        trigger_has_text=True,
    )

    reservation = budget.reservations[0]
    assert reservation["purpose"] is PaidPurpose.FOREGROUND_CHAT
    assert reservation["maximum_input_tokens"] >= 40_000  # type: ignore[operator]
    assert len(downloader.calls) == 1


@pytest.mark.asyncio
async def test_animation_budget_exhaustion_happens_before_download(
    database: Database,
) -> None:
    budget = BudgetManager(database.session_factory)
    await budget.reserve(
        purpose=PaidPurpose.FOREGROUND_CHAT,
        price=ModelPrice("fill", "test", 1_000_000, 0),
        maximum_input_tokens=10_000_000,
        maximum_output_tokens=0,
    )
    payload = make_animation_bytes("GIF", ("red", "blue"))
    downloader = RecordingDownloader(payload)
    provider = RecordingProvider()
    service = make_chat_service(database, provider, make_vision_service(downloader))
    visual = make_visual(
        filename="blocked.gif",
        content_type="image/gif",
        declared_size=len(payload),
        animated=True,
        animation_format="gif",
        animation_is_declared=True,
    )

    outcome = await service.generate(
        make_context(),
        visual_inputs=(visual,),
        trigger_has_text=False,
    )

    assert outcome.status == "budget_exhausted"
    assert downloader.calls == []
    assert provider.calls == []


@pytest.mark.asyncio
async def test_official_provider_uses_responses_input_image_shape() -> None:
    class FakeResponses:
        arguments: dict[str, object]

        async def create(self, **arguments: object) -> object:
            self.arguments = arguments
            return SimpleNamespace(
                id="resp_1",
                output_text="完成",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )

    resource = FakeResponses()
    provider = OpenAIResponsesProvider("fake-key")
    provider._client = SimpleNamespace(responses=resource)  # type: ignore[assignment]  # noqa: SLF001
    image = PreparedImage("data:image/jpeg;base64,ZmFrZQ==", "low")
    messages = (
        ProviderInputMessage("user", "歷史", "1"),
        ProviderInputMessage("user", "你看", "2", (image,)),
    )

    await provider.generate(
        model="gpt-5.6-luna",
        instructions="測試",
        messages=messages,
        maximum_output_tokens=100,
        reasoning_effort="low",
    )

    input_items = resource.arguments["input"]
    assert isinstance(input_items, list)
    assert input_items[0]["content"] == "歷史"
    assert input_items[1]["content"] == [
        {"type": "input_text", "text": "你看"},
        {
            "type": "input_image",
            "image_url": image.data_url,
            "detail": "low",
        },
    ]


@pytest.mark.asyncio
async def test_responses_marks_animation_frames_as_one_ordered_sequence() -> None:
    class FakeResponses:
        arguments: dict[str, object]

        async def create(self, **arguments: object) -> object:
            self.arguments = arguments
            return SimpleNamespace(
                id="resp_animation",
                output_text="完成",
                usage=SimpleNamespace(input_tokens=20, output_tokens=5),
            )

    resource = FakeResponses()
    provider = OpenAIResponsesProvider("fake-key")
    provider._client = SimpleNamespace(responses=resource)  # type: ignore[assignment]  # noqa: SLF001
    frames = tuple(
        PreparedImage(
            f"data:image/jpeg;base64,frame-{index}",
            "low",
            sequence_index=index,
            sequence_total=3,
            timestamp_ms=(index - 1) * 100,
            animation_format="gif",
        )
        for index in range(1, 4)
    )

    await provider.generate(
        model="gpt-5.6-luna",
        instructions="測試",
        messages=(ProviderInputMessage("user", "你看", "2", frames),),
        maximum_output_tokens=100,
        reasoning_effort="low",
    )

    input_items = resource.arguments["input"]
    content = input_items[0]["content"]
    texts = [part["text"] for part in content if part["type"] == "input_text"]
    images = [part["image_url"] for part in content if part["type"] == "input_image"]
    assert "同一個 GIF 動畫" in texts[1]
    assert texts[2:] == [
        "[動畫畫面 1/3，約 0 毫秒]",
        "[動畫畫面 2/3，約 100 毫秒]",
        "[動畫畫面 3/3，約 200 毫秒]",
    ]
    assert images == [frame.data_url for frame in frames]


def test_normal_mode_image_does_not_bypass_explicit_trigger() -> None:
    policy = ReplyTriggerPolicy(companion_cooldown=datetime.resolution)

    decision = policy.decide(
        ChannelMode.NORMAL,
        ReplySignals(channel_id=1, content="你看", has_visual=True),
    )

    assert decision.should_reply is False
    assert decision.reason == "normal_requires_explicit_trigger"


def test_companion_mode_does_not_reply_to_every_image() -> None:
    policy = ReplyTriggerPolicy(companion_cooldown=datetime.resolution)

    silent = policy.decide(
        ChannelMode.COMPANION,
        ReplySignals(channel_id=1, content="", has_visual=True),
    )
    engaged = policy.decide(
        ChannelMode.COMPANION,
        ReplySignals(channel_id=1, content="Salt 你看這張", has_visual=True),
    )

    assert silent.should_reply is False
    assert engaged.should_reply is True
    assert engaged.reason == "visual_conversation_signal"


class NoopNotifier:
    async def notify_author(self, notice: object) -> None:
        del notice

    async def notify_admins(self, notice: object) -> None:
        del notice


class NoopSegmenter:
    async def assign_message(self, discord_message_id: str) -> None:
        del discord_message_id


@pytest.mark.asyncio
async def test_database_and_logs_never_receive_base64_or_cdn_url(
    message_repository: MessageRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    source_url = "https://cdn.discordapp.com/attachments/1/123/private.png?signed=secret"
    visual = make_visual(source_url=source_url)
    handler = MessageHandler(
        repository=message_repository,
        sensitive_filter=SensitiveFilter(),
        notifier=NoopNotifier(),
        segmenter=NoopSegmenter(),
        allowed_guild_ids=frozenset({1}),
        allowed_channel_ids=frozenset({2}),
    )
    incoming = IncomingMessage(
        discord_message_id="100",
        guild_id=1,
        channel_id=2,
        author_id=3,
        author_display_name="測試者",
        content="你看",
        discord_created_at=datetime.now(UTC),
        replied_to_message_id=None,
        author_is_bot=False,
        is_own_message=False,
        visual_inputs=(visual,),
    )

    outcome = await handler.handle(incoming)
    stored = await message_repository.get_by_discord_id("100")

    assert outcome.status == "stored"
    assert stored is not None
    assert "kind=attachment" in stored.content
    assert source_url not in stored.content
    assert "base64" not in stored.content.lower()
    assert source_url not in caplog.text
    assert "base64" not in caplog.text.lower()
    assert source_url not in repr(visual)
    prepared = PreparedImage("data:image/jpeg;base64,secret-payload", "low")
    assert "secret-payload" not in repr(prepared)


@pytest.mark.asyncio
async def test_sensitive_filename_is_masked_and_blocks_normal_ai_path(
    message_repository: MessageRepository,
) -> None:
    secret = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz123456"
    visual = make_visual(filename=f"{secret}.png")
    handler = MessageHandler(
        repository=message_repository,
        sensitive_filter=SensitiveFilter(),
        notifier=NoopNotifier(),
        segmenter=NoopSegmenter(),
        allowed_guild_ids=frozenset({1}),
        allowed_channel_ids=frozenset({2}),
    )
    incoming = IncomingMessage(
        discord_message_id="101",
        guild_id=1,
        channel_id=2,
        author_id=3,
        author_display_name="測試者",
        content="Salt 你看",
        discord_created_at=datetime.now(UTC),
        replied_to_message_id=None,
        author_is_bot=False,
        is_own_message=False,
        visual_inputs=(visual,),
    )

    outcome = await handler.handle(incoming)
    stored = await message_repository.get_by_discord_id("101")

    assert outcome.status == "stored_sensitive"
    assert stored is not None and stored.is_sensitive is True
    assert secret not in stored.content
