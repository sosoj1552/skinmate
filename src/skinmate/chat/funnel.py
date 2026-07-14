"""좁히기 퍼널(1B.6) — 모호한 요청에서 다음 narrowing 질문을 고른다(PRD F6, AC-R1).

서버에 퍼널 상태를 저장하지 않는다(stateless). "이미 답변된 슬롯"은 memories 에 이미 쓰인
durable 사실(rank_memory 결과)에서 매 턴 역산한다 — 사용자가 좁히기 질문에 답하면 그 답이
fact 추출→writer 를 거쳐 memories 에 쌓이고, 다음 턴 memory_facts 에 자연히 반영되므로
대화 히스토리를 따로 재파싱할 필요가 없다.
"""

from __future__ import annotations

from skinmate.contracts.facts import FactType, RankedFact

_ORDER = ["concern", "ingredient"]
_QUESTIONS = {
    "concern": "어떤 고민이 제일 크세요? 건조·트러블·민감 중에 골라 주시면 도움이 돼요.",
    "ingredient": "혹시 피하고 싶거나 선호하는 성분이 있으신가요?",
}


def known_slots_from_memory(memory_facts: list[RankedFact]) -> set[str]:
    """회상된 기억에서 이미 답변된 퍼널 슬롯을 역산."""
    known: set[str] = set()
    for f in memory_facts:
        if f.fact_type == FactType.HAS_CONCERN:
            known.add("concern")
        elif f.fact_type in (FactType.AVOID_INGREDIENT, FactType.PREFER_INGREDIENT):
            known.add("ingredient")
    return known


def next_funnel_question(known_slots: set[str]) -> str | None:
    """아직 안 채워진 슬롯 중 우선순위가 가장 높은 것의 질문. 전부 채워졌으면 None(퍼널 완료)."""
    for slot in _ORDER:
        if slot not in known_slots:
            return _QUESTIONS[slot]
    return None


def is_funnel_question(text: str) -> bool:
    """주어진 봇 메시지가 퍼널 질문인지 판별 — 턴 배선이 '직전 턴이 퍼널이었는지'를
    history 에서 역산해 같은 턴 자동 재추천 여부를 결정하는 데 쓴다."""
    return text.strip() in _QUESTIONS.values()
