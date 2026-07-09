"""fact 추출기 테스트 — 파싱·중요도 필터·재시도 폴백(유닛) + AC-M3 평가셋(녹화 재생).

실제 Gemini 호출 대신 LLMProvider 를 만족하는 가짜/녹화 프로바이더를 주입한다(비용·재현성,
ACCEPTANCE §2). DB 불필요.
"""

from __future__ import annotations

import json
from pathlib import Path

from skinmate.contracts.facts import FactType
from skinmate.errors import LLMError
from skinmate.memory.extract import ExtractedFact, extract_facts

_FIX = Path(__file__).resolve().parents[2] / "eval" / "fixtures"
_LABELS = _FIX / "importance_labels.jsonl"
_RECORDINGS = _FIX / "llm_recordings" / "extract.json"


class _CannedProvider:
    """항상 같은 dict 를 반환하는 가짜 프로바이더."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def complete(self, system: str, prompt: str) -> str:
        return ""

    def complete_json(
        self, system: str, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        return self._payload


class _FlakyProvider:
    """앞선 N 회는 LLMError, 그 뒤로는 payload 를 반환(재시도 검증용)."""

    def __init__(self, fail_times: int, payload: dict[str, object]) -> None:
        self._fail_times = fail_times
        self._payload = payload
        self.calls = 0

    def complete(self, system: str, prompt: str) -> str:
        return ""

    def complete_json(
        self, system: str, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise LLMError("모의 파싱 실패")
        return self._payload


class _RecordedProvider:
    """발화→녹화응답 매핑. 누락 발화는 LLMError(추출기가 no-op 처리)."""

    def __init__(self, table: dict[str, dict[str, object]]) -> None:
        self._table = table

    def complete(self, system: str, prompt: str) -> str:
        return ""

    def complete_json(
        self, system: str, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        if prompt not in self._table:
            raise LLMError(f"녹화 없음: {prompt}")
        return self._table[prompt]


# ── 유닛: 파싱·필터·폴백 ────────────────────────────────────────────
def test_extract_domain_fact() -> None:
    """도메인 사실 1개를 올바른 fact_type/필드로 파싱."""
    provider = _CannedProvider(
        {
            "facts": [
                {"fact_type": "avoid_ingredient", "content": "레티놀 회피", "target_name": "레티놀"}
            ]
        }
    )
    facts = extract_facts(provider, "레티놀 싫어요")
    assert facts == [
        ExtractedFact(
            fact_type=FactType.AVOID_INGREDIENT, content="레티놀 회피", target_name="레티놀"
        )
    ]


def test_extract_filters_smalltalk() -> None:
    """잡담(빈 facts)은 저장 후보 0건으로 걸러진다(중요도 필터)."""
    provider = _CannedProvider({"facts": []})
    assert extract_facts(provider, "오늘 피곤해") == []


def test_extract_retry_then_noop() -> None:
    """2회 연속 파싱 실패 → no-op([]), 정확히 2회 시도."""
    provider = _FlakyProvider(fail_times=2, payload={"facts": []})
    assert extract_facts(provider, "레티놀 싫어요") == []
    assert provider.calls == 2


def test_extract_retry_recovers() -> None:
    """1회 실패 후 2번째 성공 → 사실 반환."""
    provider = _FlakyProvider(
        fail_times=1,
        payload={"facts": [{"fact_type": "skin_type", "content": "지성"}]},
    )
    facts = extract_facts(provider, "지성이에요")
    assert [f.fact_type for f in facts] == [FactType.SKIN_TYPE]
    assert provider.calls == 2


def test_extract_invalid_fact_type_is_noop() -> None:
    """알 수 없는 fact_type → 검증 실패 → 재시도 후 no-op."""
    provider = _CannedProvider({"facts": [{"fact_type": "made_up", "content": "x"}]})
    assert extract_facts(provider, "무언가") == []


# ── AC-M3: 저장 precision ≥ 0.85, 도메인 recall ≥ 0.85 ──────────────
def test_importance_precision_recall() -> None:
    """라벨셋(≥20 발화)에서 녹화 재생 기준 precision/recall 임계치 충족."""
    labels = [json.loads(line) for line in _LABELS.read_text(encoding="utf-8").splitlines() if line]
    recordings = json.loads(_RECORDINGS.read_text(encoding="utf-8"))
    provider = _RecordedProvider(recordings)

    assert len(labels) >= 20, "AC-M3 는 발화 20개 이상 요구"

    tp = fp = fn = 0
    for row in labels:
        stored = len(extract_facts(provider, row["utterance"])) > 0
        gold = row["domain"]
        if stored and gold:
            tp += 1
        elif stored and not gold:
            fp += 1
        elif not stored and gold:
            fn += 1

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    assert precision >= 0.85, f"저장 precision {precision:.3f} < 0.85"
    assert recall >= 0.85, f"도메인 recall {recall:.3f} < 0.85"
