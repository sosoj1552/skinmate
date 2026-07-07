"""스켈레톤 스모크: 패키지 import·설정 로드·LLMProvider 구조 적합성."""

from __future__ import annotations

import skinmate
from skinmate.config import Settings
from skinmate.llm.base import LLMProvider
from skinmate.llm.claude import ClaudeProvider


def test_package_version() -> None:
    assert skinmate.__version__ == "0.1.0"


def test_settings_defaults() -> None:
    s = Settings(_env_file=None)  # .env 무시, 기본값만
    assert s.embedder_mode in {"local", "container", "api"}
    assert s.llm_model


def test_claude_provider_conforms_to_protocol() -> None:
    provider: LLMProvider = ClaudeProvider(api_key="x", model="m")
    assert provider is not None
