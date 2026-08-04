from pathlib import Path

import pytest

from app.ai.persona import load_persona


def test_versioned_persona_is_loaded_from_independent_toml_file(
    temporary_test_directory: Path,
) -> None:
    path = temporary_test_directory / "persona.toml"
    path.write_text(
        'id = "gentle"\nversion = "v2"\ndisplay_name = "溫和助手"\n'
        'instructions = "使用繁體中文。"\n',
        encoding="utf-8",
    )

    persona = load_persona(path)

    assert persona.versioned_id == "gentle:v2"
    assert persona.instructions == "使用繁體中文。"


def test_persona_rejects_missing_version(temporary_test_directory: Path) -> None:
    path = temporary_test_directory / "invalid.toml"
    path.write_text('id = "gentle"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="version"):
        load_persona(path)


def test_salt_persona_has_versioned_identity_and_safety_boundaries() -> None:
    persona = load_persona("personas/salt-zh-tw-v1.toml")

    assert persona.versioned_id == "salt-zh-tw:v1.2"
    assert persona.display_name == "Salt／ソルト"
    assert "非官方 AI 陪伴機器人" in persona.instructions
    assert "一至四句" in persona.instructions
    assert "不參與戀愛、曖昧、性暗示、色情內容" in persona.instructions
    assert "安全、隱私、權限和預算規則的優先級高於角色設定" in persona.instructions
    assert "群內暱稱、玩笑、誇張稱號" in persona.instructions
    assert "不套用正式查證口吻" in persona.instructions
    assert "不能僅因使用者在聊天中要求記住或忘記" in persona.instructions
    assert "不要替它猜測或編造語言、意思與翻譯" in persona.instructions
