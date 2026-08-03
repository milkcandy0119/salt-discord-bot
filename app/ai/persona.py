"""載入獨立、可替換且可版本化的 AI 人設設定。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Persona:
    """只控制表達方式的人設資料。"""

    identifier: str
    version: str
    display_name: str
    instructions: str

    @property
    def versioned_id(self) -> str:
        """傳回可寫入日誌或紀錄的穩定版本識別。"""

        return f"{self.identifier}:{self.version}"


def load_persona(path: str | Path) -> Persona:
    """從 TOML 載入人設，拒絕缺少必要欄位的設定。"""

    persona_path = Path(path)
    with persona_path.open("rb") as file:
        values = tomllib.load(file)
    required = ("id", "version", "display_name", "instructions")
    if any(not isinstance(values.get(key), str) or not values[key].strip() for key in required):
        raise ValueError("人設設定必須包含非空白的 id、version、display_name 與 instructions")
    return Persona(
        identifier=values["id"].strip(),
        version=values["version"].strip(),
        display_name=values["display_name"].strip(),
        instructions=values["instructions"].strip(),
    )
