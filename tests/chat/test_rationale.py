"""근거 생성 테스트 — 인용 검증(AC-R3: 범위 밖 인용 드롭) + 근거 전무 시 LLM 호출 없는 폴백.

계약 fixture(tests/contracts/stubs.stub_retrieval_context)로 개발한다(1A.7 실물 전, WBS 지시).
"""

from __future__ import annotations

from tests.contracts.stubs import stub_retrieval_context

from skinmate.chat.rationale import FALLBACK_MESSAGE, generate_rationale
from skinmate.contracts.retrieval import RetrievalContext
from skinmate.errors import LLMError


class _CannedProvider:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.calls = 0

    def complete(self, system: str, prompt: str) -> str:
        return ""

    def complete_json(
        self, system: str, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        self.calls += 1
        return self._payload


class _NeverCallProvider:
    """호출되면 실패 — 'LLM 을 아예 안 부르는지' 증명용."""

    def complete(self, system: str, prompt: str) -> str:
        raise AssertionError("LLM 호출되면 안 됨")

    def complete_json(
        self, system: str, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        raise AssertionError("LLM 호출되면 안 됨")


class _FlakyProvider:
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
            raise LLMError("모의 실패")
        return self._payload


def test_rationale_cites_valid_path_and_memory() -> None:
    context = stub_retrieval_context()  # graph_paths[0], memory_facts[0].memory_id=101
    provider = _CannedProvider(
        {
            "response": "수분 에멀전을 추천해요.",
            "cited_graph_path_indices": [0],
            "cited_memory_ids": [101],
        }
    )
    rationale = generate_rationale(provider, context)
    assert rationale.response == "수분 에멀전을 추천해요."
    assert rationale.cited_graph_path_indices == [0]
    assert rationale.cited_memory_ids == [101]


def test_rationale_drops_out_of_range_path_index() -> None:
    """AC-R3: LLM 이 존재하지 않는 경로 인덱스(5)를 지어내면 코드가 드롭."""
    context = stub_retrieval_context()
    provider = _CannedProvider(
        {"response": "...", "cited_graph_path_indices": [0, 5], "cited_memory_ids": []}
    )
    rationale = generate_rationale(provider, context)
    assert rationale.cited_graph_path_indices == [0]


def test_rationale_drops_unknown_memory_id() -> None:
    """AC-R3: context 에 없는 memory_id(999)를 지어내면 코드가 드롭."""
    context = stub_retrieval_context()
    provider = _CannedProvider(
        {"response": "...", "cited_graph_path_indices": [], "cited_memory_ids": [101, 999]}
    )
    rationale = generate_rationale(provider, context)
    assert rationale.cited_memory_ids == [101]


def test_rationale_no_grounding_skips_llm_call() -> None:
    """graph_paths·memory_facts·products 전부 비면 LLM 을 아예 안 부르고 즉시 폴백."""
    empty_context = RetrievalContext(query="아무거나")
    rationale = generate_rationale(_NeverCallProvider(), empty_context)
    assert rationale.response == FALLBACK_MESSAGE


def test_rationale_products_only_still_generates() -> None:
    """콜드스타트: 기억·그래프 없이 후보 제품만 있어도 추천을 생성한다(신규 사용자 지원)."""
    from skinmate.contracts.refs import ProductRef

    context = RetrievalContext(
        query="산뜻한 토너 추천해줘",
        products=[ProductRef(product_id=35, name="스킨 리커버리 토너", brand="Paula's Choice")],
    )
    provider = _CannedProvider({"response": "스킨 리커버리 토너를 추천해요."})
    rationale = generate_rationale(provider, context)
    assert provider.calls == 1
    assert rationale.response == "스킨 리커버리 토너를 추천해요."
    assert rationale.cited_graph_path_indices == []
    assert rationale.cited_memory_ids == []


def test_rationale_retries_then_recovers() -> None:
    context = stub_retrieval_context()
    provider = _FlakyProvider(fail_times=1, payload={"response": "재시도 성공"})
    rationale = generate_rationale(provider, context)
    assert rationale.response == "재시도 성공"
    assert provider.calls == 2


def test_rationale_gives_up_after_two_falls_back() -> None:
    context = stub_retrieval_context()
    provider = _FlakyProvider(fail_times=2, payload={"response": "안 씀"})
    rationale = generate_rationale(provider, context)
    assert rationale.response == FALLBACK_MESSAGE
    assert provider.calls == 2
