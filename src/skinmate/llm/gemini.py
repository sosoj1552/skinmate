"""Gemini(Google AI Studio) 기반 LLMProvider 구현 — 기본 프로바이더.

무료 `google-genai` SDK 로 `gemini-2.0-flash` 를 호출한다. 구조적으로 `LLMProvider`(base.py)를
만족하므로 나머지 코드는 이 구현을 몰라도 된다(⭐9c). SDK 는 지연 임포트 — google-genai 미설치
환경(테스트에서 가짜 프로바이더 사용)에서도 이 모듈을 import 할 수 있게 한다.
"""

from __future__ import annotations

import json

from skinmate.errors import LLMError

DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiProvider:
    """google-genai 기반 LLMProvider. `LLMProvider` Protocol 을 구조적으로 만족한다."""

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        self._api_key = api_key
        self._model = model

    def complete(self, system: str, prompt: str) -> str:
        """자유 텍스트 응답 생성."""
        return self._generate(system, prompt, json_mode=False)

    def complete_json(
        self, system: str, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        """JSON 강제 구조화 출력. 스키마를 프롬프트에 덧붙여 형식을 유도, 파싱 실패 시 LLMError."""
        full_prompt = f"{prompt}\n\n[출력 JSON 스키마]\n{json.dumps(schema, ensure_ascii=False)}"
        text = self._generate(system, full_prompt, json_mode=True)
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"Gemini JSON 파싱 실패: {exc}") from exc
        if not isinstance(obj, dict):
            raise LLMError("Gemini JSON 최상위가 object 가 아님")
        return obj

    def _generate(self, system: str, prompt: str, *, json_mode: bool) -> str:
        """단일 generate_content 호출. 빈 응답은 LLMError 로 승격한다."""
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self._api_key)
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json" if json_mode else None,
        )
        resp = client.models.generate_content(model=self._model, contents=prompt, config=config)
        text = resp.text
        if not text:
            raise LLMError("Gemini 빈 응답")
        return str(text)
