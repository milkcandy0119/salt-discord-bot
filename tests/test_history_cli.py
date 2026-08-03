from app import history_cli
from app.config import Settings


def test_history_cli_without_token_fails_before_external_connection(
    monkeypatch,
    capsys,
) -> None:
    """缺少 Discord token 時不得嘗試建立歷史來源。"""

    monkeypatch.setattr(
        history_cli,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            discord_allowed_guild_ids="1",
            discord_allowed_channel_ids="2",
        ),
    )

    exit_code = history_cli.main(["analyze", "--limit-per-channel", "10"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "DISCORD_BOT_TOKEN" in captured.err
    assert captured.out == ""


def test_history_cli_rejects_non_positive_limit_before_external_connection(
    monkeypatch,
    capsys,
) -> None:
    """不合法範圍應在任何 Discord 讀取前被拒絕。"""

    monkeypatch.setattr(
        history_cli,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            discord_bot_token="fake-token",
            discord_allowed_guild_ids="1",
            discord_allowed_channel_ids="2",
        ),
    )

    exit_code = history_cli.main(["analyze", "--limit-per-channel", "0"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "上限必須大於零" in captured.err
    assert captured.out == ""
