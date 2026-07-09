"""라이브 스모크 — 실제 Gemini 호출로 fact 추출기가 의도대로 동작하는지 확인.

기본 CI/유닛은 녹화 재생(test_extract.py)으로 돌고, 이 파일은 소량 라이브 스모크다
(ACCEPTANCE §2). `GEMINI_API_KEY` 미설정 시 전체 skip — 비용·키 없는 환경 보호.
실행: .env 또는 환경변수에 GEMINI_API_KEY 설정 후 `pytest tests/memory/test_extract_live.py`.
"""

from __future__ import annotations

import pytest

from skinmate.config import settings
from skinmate.contracts.facts import FactType
from skinmate.llm.gemini import GeminiProvider
from skinmate.memory.extract import extract_facts

pytestmark = pytest.mark.skipif(
    not settings.gemini_api_key, reason="GEMINI_API_KEY 미설정 — 라이브 스모크 skip"
)


@pytest.fixture(scope="module")
def provider() -> GeminiProvider:
    return GeminiProvider(settings.gemini_api_key, settings.llm_model)


def test_live_extracts_avoid_ingredient(provider: GeminiProvider) -> None:
    """구체 회피 성분 발화 → avoid_ingredient 사실이 실제로 추출된다."""
    facts = extract_facts(provider, "저는 레티놀 쓰면 얼굴이 따가워서 못 써요")
    assert facts, "도메인 사실이 최소 1개는 추출돼야 함"
    assert any(f.fact_type == FactType.AVOID_INGREDIENT for f in facts)
    assert any("레티놀" in (f.target_name or f.content) for f in facts)


def test_live_filters_smalltalk(provider: GeminiProvider) -> None:
    """비도메인 잡담 → 중요도 필터로 걸러져 빈 결과."""
    assert extract_facts(provider, "아 오늘 진짜 피곤하고 졸리다") == []
