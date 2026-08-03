"""不需外部服務的敏感資料偵測與遮罩。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScanResult:
    """敏感資料掃描結果，只保留遮罩後內容。"""

    masked_content: str
    categories: tuple[str, ...]

    @property
    def is_sensitive(self) -> bool:
        """指出是否偵測到至少一項敏感資料。"""

        return bool(self.categories)


@dataclass(frozen=True, slots=True)
class _Rule:
    pattern: re.Pattern[str]
    category: str
    replacement: str
    value_group: str | None = None


@dataclass(frozen=True, slots=True)
class _Match:
    start: int
    end: int
    category: str
    replacement: str
    priority: int


class SensitiveFilter:
    """以可重現的本機規則尋找並遮罩常見祕密。"""

    _rules = (
        _Rule(
            re.compile(
                r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
                r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
                re.DOTALL,
            ),
            "private_key",
            "[PRIVATE_KEY_REDACTED]",
        ),
        _Rule(
            re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
            "openai_api_key",
            "[OPENAI_API_KEY_REDACTED]",
        ),
        _Rule(
            re.compile(
                r"\b(?:mfa\.[A-Za-z0-9_-]{20,}|"
                r"[A-Za-z\d]{23,28}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{25,40})\b"
            ),
            "discord_token",
            "[DISCORD_TOKEN_REDACTED]",
        ),
        _Rule(
            re.compile(
                r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|passwd|secret)"
                r"\b\s*[:=]\s*(?P<value>['\"]?[^\s,'\"]{6,}['\"]?)",
                re.IGNORECASE,
            ),
            "named_secret",
            "[SECRET_REDACTED]",
            value_group="value",
        ),
    )

    def scan(self, content: str) -> ScanResult:
        """掃描原文，傳回不含已識別祕密的內容與分類。"""

        candidates: list[_Match] = []
        for priority, rule in enumerate(self._rules):
            for match in rule.pattern.finditer(content):
                start, end = match.span(rule.value_group) if rule.value_group else match.span()
                candidates.append(_Match(start, end, rule.category, rule.replacement, priority))

        accepted: list[_Match] = []
        ordered_candidates = sorted(
            candidates,
            key=lambda item: (item.start, item.priority, -item.end),
        )
        for candidate in ordered_candidates:
            if any(candidate.start < item.end and candidate.end > item.start for item in accepted):
                continue
            accepted.append(candidate)

        masked_content = content
        for match in sorted(accepted, key=lambda item: item.start, reverse=True):
            masked_content = (
                masked_content[: match.start] + match.replacement + masked_content[match.end :]
            )

        ordered_matches = sorted(accepted, key=lambda item: item.start)
        categories = tuple(dict.fromkeys(item.category for item in ordered_matches))
        return ScanResult(masked_content=masked_content, categories=categories)
