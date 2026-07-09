"""fact 추출기(1B.1) — 발화에서 저장할 도메인 사실만 뽑는다(중요도 필터 포함, AC-M3).

LLM(Gemini 기본)에 구조화 출력을 요청하고, 잡담·비도메인은 빈 결과로 걸러낸다(중요도 필터 =
LLM 이 facts 를 비워 반환). JSON 파싱·검증 실패 시 **1회 재시도 후 no-op**(그 턴 저장 스킵) —
PRD F1 폴백. 응답은 이미 나갔으므로 대화는 계속된다. 결과는 후속 1B.2(CRUD 판정)가 소비한다.
"""

from __future__ import annotations

from pathlib import Path

import structlog
from pydantic import BaseModel, ValidationError

from skinmate.contracts.facts import FactType
from skinmate.errors import LLMError
from skinmate.llm.base import LLMProvider

logger = structlog.get_logger(__name__)

_PROMPT = (
    Path(__file__).resolve().parent.parent / "llm" / "prompts" / "extract_facts.txt"
).read_text(encoding="utf-8")

_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "fact_type": {"type": "string", "enum": [t.value for t in FactType]},
                    "content": {"type": "string"},
                    "target_name": {"type": ["string", "null"]},
                    "season": {"type": ["string", "null"]},
                },
                "required": ["fact_type", "content"],
            },
        }
    },
    "required": ["facts"],
}


class ExtractedFact(BaseModel):
    """추출된 저장-후보 사실(저장 전, memory_id 없음). 1B.2 CRUD 판정의 입력."""

    fact_type: FactType
    content: str
    target_name: str | None = None  # 성분/브랜드/고민 이름(다리 연결용 원시 값)
    season: str | None = None


def extract_facts(provider: LLMProvider, utterance: str) -> list[ExtractedFact]:
    """발화에서 도메인 사실을 뽑는다. 잡담이면 [], 파싱 실패 2회면 no-op([]).

    provider 는 주입(테스트에서 녹화 프로바이더로 교체). 저장/CRUD 는 여기서 하지 않는다.
    """
    for attempt in (1, 2):
        try:
            raw = provider.complete_json(_PROMPT, utterance, _SCHEMA)
            return _parse(raw)
        except (LLMError, ValidationError, ValueError) as exc:
            logger.warning("fact_extract_retry", attempt=attempt, error=str(exc))
    logger.error("fact_extract_gave_up", utterance=utterance)
    return []


def _parse(raw: dict[str, object]) -> list[ExtractedFact]:
    """LLM 응답 dict → ExtractedFact 목록. 형식 위반은 예외로 올려 재시도/no-op 를 유발."""
    facts_raw = raw.get("facts", [])
    if not isinstance(facts_raw, list):
        raise ValueError("facts 는 배열이어야 함")
    return [ExtractedFact.model_validate(item) for item in facts_raw]
