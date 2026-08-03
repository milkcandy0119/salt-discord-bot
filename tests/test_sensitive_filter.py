from app.security.sensitive_filter import SensitiveFilter


def test_openai_key_is_masked_without_retaining_the_secret() -> None:
    secret = "sk-" + "proj-abcdefghijklmnopqrstuvwxyz123456"

    result = SensitiveFilter().scan(f"請檢查這個 key：{secret}")

    assert result.is_sensitive is True
    assert result.categories == ("openai_api_key",)
    assert secret not in result.masked_content
    assert "[OPENAI_API_KEY_REDACTED]" in result.masked_content


def test_private_key_block_is_fully_masked() -> None:
    secret = "-----BEGIN PRIVATE KEY-----\nabc123\n-----END PRIVATE KEY-----"

    result = SensitiveFilter().scan(secret)

    assert result.categories == ("private_key",)
    assert secret not in result.masked_content
    assert result.masked_content == "[PRIVATE_KEY_REDACTED]"


def test_ordinary_message_is_unchanged() -> None:
    content = "今天要討論資料庫 migration。"

    result = SensitiveFilter().scan(content)

    assert result.is_sensitive is False
    assert result.categories == ()
    assert result.masked_content == content


def test_discord_token_and_named_password_are_masked() -> None:
    discord_token = f"{'M' * 24}.abcdef.{'x' * 27}"
    password = "correct-horse-battery-staple"

    result = SensitiveFilter().scan(
        f"discord={discord_token}\npassword={password}"
    )

    assert result.categories == ("discord_token", "named_secret")
    assert discord_token not in result.masked_content
    assert password not in result.masked_content
