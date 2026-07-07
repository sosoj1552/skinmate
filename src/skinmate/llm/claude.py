"""Claude API 기반 LLMProvider 구현(스켈레톤). 실제 호출은 후속 작업에서 채운다."""

from __future__ import annotations


class ClaudeProvider:
    """anthropic SDK 기반 LLMProvider 구현. 구조적으로 `LLMProvider`를 만족한다."""

    def __init__(self, api_key: str, model: str) -> None:
        self._api_key = api_key
        self._model = model

    def complete(self, system: str, prompt: str) -> str:
        raise NotImplementedError("ClaudeProvider.complete — 후속 작업")

    def complete_json(
        self, system: str, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        raise NotImplementedError("ClaudeProvider.complete_json — 후속 작업")
