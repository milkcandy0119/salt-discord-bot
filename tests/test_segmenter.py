from datetime import UTC, datetime, timedelta

import pytest

from app.conversations.segmenter import ConversationSegmenter
from app.storage.database import Database
from app.storage.repositories import MessageRepository, NewMessage


async def save_message(
    repository: MessageRepository,
    *,
    message_id: str,
    author_id: str,
    created_at: datetime,
    reply_to: str | None = None,
) -> None:
    await repository.save(
        NewMessage(
            discord_message_id=message_id,
            guild_id="1",
            channel_id="2",
            author_id=author_id,
            author_display_name=f"使用者 {author_id}",
            content=f"訊息 {message_id}",
            discord_created_at=created_at,
            received_at=created_at,
            replied_to_message_id=reply_to,
            is_bot=False,
            is_sensitive=False,
            sensitive_categories=(),
        )
    )


def make_segmenter(database: Database) -> ConversationSegmenter:
    return ConversationSegmenter(
        database.session_factory,
        archive_after=timedelta(minutes=30),
        implicit_continuation_window=timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_unique_recent_segment_for_same_author_is_continued(
    database: Database,
    message_repository: MessageRepository,
) -> None:
    segmenter = make_segmenter(database)
    start = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    await save_message(message_repository, message_id="root", author_id="10", created_at=start)
    root = await segmenter.assign_message("root")
    await save_message(
        message_repository,
        message_id="follow-up",
        author_id="10",
        created_at=start + timedelta(minutes=2),
    )

    follow_up = await segmenter.assign_message("follow-up")

    assert follow_up.segment_id == root.segment_id
    assert follow_up.created_segment is False


@pytest.mark.asyncio
async def test_new_participant_without_reply_starts_a_new_topic(
    database: Database,
    message_repository: MessageRepository,
) -> None:
    segmenter = make_segmenter(database)
    start = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    await save_message(message_repository, message_id="topic-a", author_id="10", created_at=start)
    topic_a = await segmenter.assign_message("topic-a")
    await save_message(
        message_repository,
        message_id="topic-b",
        author_id="20",
        created_at=start + timedelta(minutes=1),
    )

    topic_b = await segmenter.assign_message("topic-b")

    assert topic_b.segment_id != topic_a.segment_id
    assert topic_b.created_segment is True


@pytest.mark.asyncio
async def test_same_author_after_continuation_window_starts_a_new_topic(
    database: Database,
    message_repository: MessageRepository,
) -> None:
    segmenter = make_segmenter(database)
    start = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    await save_message(message_repository, message_id="topic-a", author_id="10", created_at=start)
    topic_a = await segmenter.assign_message("topic-a")
    await save_message(
        message_repository,
        message_id="topic-b",
        author_id="10",
        created_at=start + timedelta(minutes=6),
    )

    topic_b = await segmenter.assign_message("topic-b")

    assert topic_b.segment_id != topic_a.segment_id
    assert topic_b.created_segment is True


@pytest.mark.asyncio
async def test_two_topics_can_coexist_and_replies_return_to_the_correct_one(
    database: Database,
    message_repository: MessageRepository,
) -> None:
    segmenter = make_segmenter(database)
    start = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    await save_message(message_repository, message_id="topic-a", author_id="10", created_at=start)
    topic_a = await segmenter.assign_message("topic-a")
    await save_message(
        message_repository,
        message_id="topic-b",
        author_id="20",
        created_at=start + timedelta(minutes=1),
    )
    topic_b = await segmenter.assign_message("topic-b")
    await save_message(
        message_repository,
        message_id="reply-a",
        author_id="30",
        created_at=start + timedelta(minutes=2),
        reply_to="topic-a",
    )
    await save_message(
        message_repository,
        message_id="reply-b",
        author_id="40",
        created_at=start + timedelta(minutes=3),
        reply_to="topic-b",
    )

    reply_a = await segmenter.assign_message("reply-a")
    reply_b = await segmenter.assign_message("reply-b")

    assert topic_a.segment_id != topic_b.segment_id
    assert reply_a.segment_id == topic_a.segment_id
    assert reply_b.segment_id == topic_b.segment_id


@pytest.mark.asyncio
async def test_reply_to_old_message_reopens_archived_segment(
    database: Database,
    message_repository: MessageRepository,
) -> None:
    segmenter = make_segmenter(database)
    start = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    await save_message(message_repository, message_id="old-root", author_id="10", created_at=start)
    original = await segmenter.assign_message("old-root")
    archived_count = await segmenter.archive_inactive(start + timedelta(minutes=31))
    await save_message(
        message_repository,
        message_id="late-reply",
        author_id="20",
        created_at=start + timedelta(minutes=32),
        reply_to="old-root",
    )

    reply = await segmenter.assign_message("late-reply")
    state = await segmenter.get_segment_state(original.segment_id)

    assert archived_count == 1
    assert reply.segment_id == original.segment_id
    assert reply.reopened_segment is True
    assert state == "active"


@pytest.mark.asyncio
async def test_unknown_reply_and_ambiguous_active_topics_create_new_segments(
    database: Database,
    message_repository: MessageRepository,
) -> None:
    segmenter = make_segmenter(database)
    start = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    await save_message(message_repository, message_id="first", author_id="10", created_at=start)
    first = await segmenter.assign_message("first")
    await save_message(
        message_repository,
        message_id="unknown-reply",
        author_id="10",
        created_at=start + timedelta(minutes=1),
        reply_to="not-imported",
    )
    second = await segmenter.assign_message("unknown-reply")
    await save_message(
        message_repository,
        message_id="ambiguous",
        author_id="10",
        created_at=start + timedelta(minutes=2),
    )

    third = await segmenter.assign_message("ambiguous")

    assert len({first.segment_id, second.segment_id, third.segment_id}) == 3


@pytest.mark.asyncio
async def test_segment_archives_after_thirty_minutes_without_new_messages(
    database: Database,
    message_repository: MessageRepository,
) -> None:
    segmenter = make_segmenter(database)
    start = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    await save_message(message_repository, message_id="root", author_id="10", created_at=start)
    assignment = await segmenter.assign_message("root")

    before_threshold = await segmenter.archive_inactive(start + timedelta(minutes=29))
    at_threshold = await segmenter.archive_inactive(start + timedelta(minutes=30))

    assert before_threshold == 0
    assert at_threshold == 1
    assert await segmenter.get_segment_state(assignment.segment_id) == "archived"


@pytest.mark.asyncio
async def test_assigning_same_message_twice_is_idempotent(
    database: Database,
    message_repository: MessageRepository,
) -> None:
    segmenter = make_segmenter(database)
    start = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    await save_message(message_repository, message_id="root", author_id="10", created_at=start)

    first = await segmenter.assign_message("root")
    second = await segmenter.assign_message("root")

    assert second.segment_id == first.segment_id
    assert second.created_segment is False
    assert await segmenter.count_segments() == 1


@pytest.mark.asyncio
async def test_startup_recovery_assigns_messages_saved_before_interruption(
    database: Database,
    message_repository: MessageRepository,
) -> None:
    segmenter = make_segmenter(database)
    start = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)
    await save_message(message_repository, message_id="pending-a", author_id="10", created_at=start)
    await save_message(
        message_repository,
        message_id="pending-b",
        author_id="10",
        created_at=start + timedelta(minutes=1),
        reply_to="pending-a",
    )

    recovered = await segmenter.assign_pending_messages()
    first = await message_repository.get_by_discord_id("pending-a")
    second = await message_repository.get_by_discord_id("pending-b")

    assert recovered == 2
    assert first is not None and second is not None
    assert first.segment_id == second.segment_id
    assert first.processing_status == "segmented"
    assert second.processing_status == "segmented"
