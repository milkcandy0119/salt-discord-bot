from datetime import UTC, datetime, timedelta

import pytest

from app.bot.channel_modes import (
    ChannelMode,
    ChannelModeResolver,
    ReplySignals,
    ReplyTriggerPolicy,
    TriggerKind,
)

NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def make_policy() -> ReplyTriggerPolicy:
    return ReplyTriggerPolicy(companion_cooldown=timedelta(seconds=120))


def test_channel_modes_use_ids_and_default_allowed_channels_to_normal() -> None:
    resolver = ChannelModeResolver(
        allowed_channel_ids=frozenset({10, 20}),
        companion_channel_ids=frozenset({20}),
    )

    assert resolver.resolve(10) is ChannelMode.NORMAL
    assert resolver.resolve(20) is ChannelMode.COMPANION
    assert resolver.resolve(30) is None


def test_companion_channel_must_also_be_in_storage_allowlist() -> None:
    with pytest.raises(ValueError, match="白名單"):
        ChannelModeResolver(
            allowed_channel_ids=frozenset({10}),
            companion_channel_ids=frozenset({20}),
        )


def test_normal_mode_only_accepts_explicit_triggers() -> None:
    policy = make_policy()

    ordinary = policy.decide(
        ChannelMode.NORMAL,
        ReplySignals(channel_id=10, content="大家午安", now=NOW),
    )
    mention = policy.decide(
        ChannelMode.NORMAL,
        ReplySignals(channel_id=10, content="可以幫忙嗎", mentioned_bot=True, now=NOW),
    )

    assert ordinary.should_reply is False
    assert mention.should_reply is True
    assert mention.kind is TriggerKind.MENTION


def test_companion_mode_uses_free_question_rule_without_ai_classifier() -> None:
    decision = make_policy().decide(
        ChannelMode.COMPANION,
        ReplySignals(
            channel_id=20,
            content="這個錯誤要怎麼處理？",
            recent_human_author_ids=frozenset({1}),
            now=NOW,
        ),
    )

    assert decision.should_reply is True
    assert decision.kind is TriggerKind.COMPANION
    assert decision.reason == "question_or_help_request"


def test_companion_mode_does_not_join_busy_multi_person_chat() -> None:
    decision = make_policy().decide(
        ChannelMode.COMPANION,
        ReplySignals(
            channel_id=20,
            content="你們覺得要怎麼做？",
            recent_human_author_ids=frozenset({1, 2}),
            now=NOW,
        ),
    )

    assert decision.should_reply is False
    assert decision.reason == "multiple_humans_talking"


def test_recent_bot_participation_allows_conversation_continuation() -> None:
    decision = make_policy().decide(
        ChannelMode.COMPANION,
        ReplySignals(
            channel_id=20,
            content="那我先照這樣做",
            recent_human_author_ids=frozenset({1}),
            bot_spoke_recently=True,
            now=NOW,
        ),
    )

    assert decision.should_reply is True
    assert decision.reason == "recent_bot_continuation"


def test_companion_cooldown_blocks_only_automatic_reply() -> None:
    policy = make_policy()
    signals = ReplySignals(
        channel_id=20,
        content="還能怎麼處理？",
        recent_human_author_ids=frozenset({1}),
        last_companion_reply_at=NOW - timedelta(seconds=30),
        now=NOW,
    )

    automatic = policy.decide(ChannelMode.COMPANION, signals)
    explicit = policy.decide(
        ChannelMode.COMPANION,
        ReplySignals(
            channel_id=20,
            content=signals.content,
            mentioned_bot=True,
            last_companion_reply_at=signals.last_companion_reply_at,
            now=NOW,
        ),
    )

    assert automatic.should_reply is False
    assert automatic.reason == "companion_cooldown"
    assert explicit.should_reply is True
    assert explicit.kind is TriggerKind.MENTION


def test_sticker_only_message_does_not_trigger_companion_ai() -> None:
    decision = make_policy().decide(
        ChannelMode.COMPANION,
        ReplySignals(
            channel_id=20,
            content="",
            recent_human_author_ids=frozenset({1}),
            bot_spoke_recently=True,
            now=NOW,
        ),
    )

    assert decision.should_reply is False
    assert decision.reason == "empty_content"
