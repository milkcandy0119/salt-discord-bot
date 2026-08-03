from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from app.ai.budget_manager import BudgetManager, ModelPrice, PaidPurpose
from app.ai.chat_service import (
    ChatService,
    OpenAIResponsesProvider,
    ProviderCallError,
    ProviderChatResponse,
)
from app.ai.persona import Persona
from app.conversations.context_builder import ChatContext, ProviderInputMessage
from app.security.sensitive_filter import SensitiveFilter
from app.storage.database import Database

CHAT_PRICE = ModelPrice(
    model_name="gpt-5.6-luna",
    price_version="openai-2026-08-03",
    input_microusd_per_million_tokens=1_000_000,
    output_microusd_per_million_tokens=6_000_000,
)
PERSONA = Persona(
    identifier="test",
    version="v1",
    display_name="測試助手",
    instructions="使用溫和的繁體中文。",
)


@dataclass
class FakeProvider:
    response: ProviderChatResponse = ProviderChatResponse(
        response_id="resp_1",
        output_text="這是測試回覆。",
        input_tokens=120,
        output_tokens=30,
    )
    calls: list[dict[str, object]] = field(default_factory=list)

    async def generate(self, **arguments: object) -> ProviderChatResponse:
        self.calls.append(arguments)
        return self.response


@dataclass
class FailingProvider:
    error: ProviderCallError

    async def generate(self, **arguments: object) -> ProviderChatResponse:
        del arguments
        raise self.error


def make_context(content: str = "可以幫我嗎？") -> ChatContext:
    message = ProviderInputMessage(
        role="user",
        content=content,
        discord_message_id="100",
    )
    return ChatContext(trigger_message_id="100", messages=(message,), character_count=len(content))


def make_service(
    database: Database,
    provider: FakeProvider | FailingProvider | None,
) -> ChatService:
    return ChatService(
        provider=provider,
        budget_manager=BudgetManager(database.session_factory),
        price=CHAT_PRICE,
        persona=PERSONA,
        sensitive_filter=SensitiveFilter(),
        maintenance_message="目前 AI 回覆暫時無法使用，請稍後再試。",
        maximum_output_tokens=800,
        reasoning_effort="low",
    )


def test_official_client_disables_hidden_retries_for_budget_safety() -> None:
    provider = OpenAIResponsesProvider("fake-test-key")

    assert provider._client.max_retries == 0  # noqa: SLF001


@pytest.mark.asyncio
async def test_official_provider_disables_automatic_prompt_cache_writes() -> None:
    class FakeResponsesResource:
        def __init__(self) -> None:
            self.arguments: dict[str, object] = {}

        async def create(self, **arguments: object) -> object:
            self.arguments = arguments
            return SimpleNamespace(
                id="resp_1",
                output_text="完成",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )

    resource = FakeResponsesResource()
    provider = OpenAIResponsesProvider("fake-test-key")
    provider._client = SimpleNamespace(responses=resource)  # type: ignore[assignment]  # noqa: SLF001

    await provider.generate(
        model="gpt-5.6-luna",
        instructions="測試",
        messages=make_context().messages,
        maximum_output_tokens=800,
        reasoning_effort="low",
    )

    assert resource.arguments["prompt_cache_options"] == {"mode": "explicit"}


@pytest.mark.asyncio
async def test_successful_response_is_settled_with_actual_usage(database: Database) -> None:
    provider = FakeProvider()
    service = make_service(database, provider)

    outcome = await service.generate(make_context())
    call = await BudgetManager(database.session_factory).get_call(outcome.reservation_id or "")

    assert outcome.status == "generated"
    assert outcome.content == "這是測試回覆。"
    assert call is not None
    assert call.status == "settled"
    assert call.input_tokens == 120
    assert call.output_tokens == 30
    assert provider.calls[0]["model"] == "gpt-5.6-luna"
    assert provider.calls[0]["reasoning_effort"] == "low"
    assert "test:v1" in str(provider.calls[0]["instructions"])


@pytest.mark.asyncio
async def test_missing_openai_client_returns_maintenance_without_reservation(
    database: Database,
) -> None:
    outcome = await make_service(database, None).generate(make_context())
    snapshot = await BudgetManager(database.session_factory).get_snapshot()

    assert outcome.status == "openai_not_configured"
    assert outcome.reservation_id is None
    assert snapshot.global_committed_microusd == 0


@pytest.mark.asyncio
async def test_sensitive_context_is_blocked_before_reservation(database: Database) -> None:
    secret = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz123456"
    provider = FakeProvider()

    outcome = await make_service(database, provider).generate(make_context(secret))

    assert outcome.status == "blocked_sensitive_input"
    assert provider.calls == []
    snapshot = await BudgetManager(database.session_factory).get_snapshot()
    assert snapshot.global_committed_microusd == 0


@pytest.mark.asyncio
async def test_sensitive_persona_is_blocked_before_reservation(database: Database) -> None:
    secret = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz123456"
    service = ChatService(
        provider=FakeProvider(),
        budget_manager=BudgetManager(database.session_factory),
        price=CHAT_PRICE,
        persona=Persona("unsafe", "v1", "不安全人設", secret),
        sensitive_filter=SensitiveFilter(),
        maintenance_message="目前 AI 回覆暫時無法使用，請稍後再試。",
        maximum_output_tokens=800,
        reasoning_effort="low",
    )

    outcome = await service.generate(make_context())

    assert outcome.status == "blocked_sensitive_input"
    snapshot = await BudgetManager(database.session_factory).get_snapshot()
    assert snapshot.global_committed_microusd == 0


@pytest.mark.asyncio
async def test_timeout_keeps_reservation_as_usage_uncertain(database: Database) -> None:
    provider = FailingProvider(
        ProviderCallError("api_timeout", usage_may_be_billed=True)
    )

    outcome = await make_service(database, provider).generate(make_context())
    call = await BudgetManager(database.session_factory).get_call(outcome.reservation_id or "")

    assert outcome.status == "provider_error"
    assert call is not None and call.status == "usage_uncertain"


@pytest.mark.asyncio
async def test_known_unbilled_failure_releases_reservation(database: Database) -> None:
    provider = FailingProvider(
        ProviderCallError("request_not_sent", usage_may_be_billed=False)
    )

    outcome = await make_service(database, provider).generate(make_context())
    call = await BudgetManager(database.session_factory).get_call(outcome.reservation_id or "")

    assert call is not None and call.status == "released_unbilled"
    snapshot = await BudgetManager(database.session_factory).get_snapshot()
    assert snapshot.global_reserved_microusd == 0


@pytest.mark.asyncio
async def test_missing_usage_is_never_assumed_to_be_zero(database: Database) -> None:
    provider = FakeProvider(
        response=ProviderChatResponse(
            response_id="resp_missing_usage",
            output_text="仍可顯示的回覆",
            input_tokens=None,
            output_tokens=None,
        )
    )

    outcome = await make_service(database, provider).generate(make_context())
    call = await BudgetManager(database.session_factory).get_call(outcome.reservation_id or "")

    assert outcome.status == "generated_usage_uncertain"
    assert call is not None and call.status == "usage_uncertain"


@pytest.mark.asyncio
async def test_sensitive_model_output_is_not_returned_to_discord(database: Database) -> None:
    secret = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz123456"
    provider = FakeProvider(
        response=ProviderChatResponse(
            response_id="resp_secret",
            output_text=f"不應傳送 {secret}",
            input_tokens=100,
            output_tokens=20,
        )
    )

    outcome = await make_service(database, provider).generate(make_context())

    assert outcome.status == "blocked_model_output"
    assert secret not in outcome.content


@pytest.mark.asyncio
async def test_exhausted_budget_returns_fixed_maintenance_message(database: Database) -> None:
    manager = BudgetManager(database.session_factory)
    expensive_price = ModelPrice(
        model_name="fake",
        price_version="test",
        input_microusd_per_million_tokens=1_000_000,
        output_microusd_per_million_tokens=0,
    )
    await manager.reserve(
        purpose=PaidPurpose.FOREGROUND_CHAT,
        price=expensive_price,
        maximum_input_tokens=10_000_000,
        maximum_output_tokens=0,
    )

    outcome = await make_service(database, FakeProvider()).generate(make_context())

    assert outcome.status == "budget_exhausted"
    assert outcome.content == "目前 AI 回覆暫時無法使用，請稍後再試。"


@pytest.mark.asyncio
async def test_repeated_persona_name_prefix_is_removed(database: Database) -> None:
    provider = FakeProvider(
        response=ProviderChatResponse(
            response_id="resp_salt_prefix",
            output_text="Salt：Salt: 先慢慢來就好。[*耳朵動了一下*]",
            input_tokens=100,
            output_tokens=20,
        )
    )
    service = ChatService(
        provider=provider,
        budget_manager=BudgetManager(database.session_factory),
        price=CHAT_PRICE,
        persona=Persona(
            "salt-zh-tw",
            "v1",
            "Salt／ソルト",
            "使用平靜的臺灣繁體中文。",
        ),
        sensitive_filter=SensitiveFilter(),
        maintenance_message="目前 AI 回覆暫時無法使用，請稍後再試。",
        maximum_output_tokens=800,
        reasoning_effort="low",
    )

    outcome = await service.generate(make_context())

    assert outcome.status == "generated"
    assert outcome.content == "先慢慢來就好。[*耳朵動了一下*]"
