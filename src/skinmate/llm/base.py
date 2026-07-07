"""LLM 프로바이더 인터페이스. 코드는 이 인터페이스만 의존하고 구현은 교체 가능(⭐9c)."""

from __future__ import annotations

from typing import Protocol


class LLMProvider(Protocol):
    """텍스트 생성 + 구조화(JSON) 출력 인터페이스."""

    def complete(self, system: str, prompt: str) -> str:
        """자유 텍스트 응답 생성."""
        ...

    def complete_json(
        self, system: str, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        """JSON 스키마를 강제한 구조화 출력. 파싱 실패 시 LLMError."""
        ...
