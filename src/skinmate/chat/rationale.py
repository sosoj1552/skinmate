"""근거 생성(1B.6) — RetrievalContext 의 graph_paths·memory_facts 만 인용해 추천 응답을 만든다.

PRD F6: "근거 생성은 retrieve 가 준 graph_paths·memory_facts 만 인용. 없는 사실 생성 금지"
(AC-R3). 프롬프트로 환각을 금지할 뿐 아니라, LLM 이 인용했다고 주장한 그래프 경로 인덱스·
memory_id 를 실제 context 범위와 대조해 **범위 밖 인용은 코드로 드롭**한다(⭐7, 계약 기반
검증 — 텍스트 전체를 파싱해 검증하는 대신, 인용 ID 자체를 구조화 출력에 강제해 검증 가능하게
만드는 설계). 근거로 쓸 경로·기억이 전무하면 LLM 을 부르지 않고 즉시 폴백(PRD F6 예외처리).
"""

from __future__ import annotations

from pathlib import Path

import structlog
from pydantic import BaseModel, ValidationError

from skinmate.contracts.graph import GraphPath
from skinmate.contracts.retrieval import RetrievalContext
from skinmate.errors import LLMError
from skinmate.llm.base import LLMProvider

logger = structlog.get_logger(__name__)

_PROMPT = (
    Path(__file__).resolve().parent.parent / "llm" / "prompts" / "generate_rationale.txt"
).read_text(encoding="utf-8")

_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "response": {"type": "string"},
        "cited_graph_path_indices": {"type": "array", "items": {"type": "integer"}},
        "cited_memory_ids": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["response"],
}

FALLBACK_MESSAGE = "지금 가진 정보로는 확실한 추천을 드리기 어려워요. 몇 가지 더 여쭤볼게요."


class Rationale(BaseModel):
    """생성된 응답 + 실제 인용한 근거(검증 통과분만)."""

    response: str
    cited_graph_path_indices: list[int] = []
    cited_memory_ids: list[int] = []


def generate_rationale(provider: LLMProvider, context: RetrievalContext) -> Rationale:
    """근거 생성. 경로·기억·후보 제품이 전부 비었을 때만 LLM 호출 없이 즉시 폴백(PRD F6 예외처리).

    기억·그래프가 없는 신규 사용자(콜드스타트)라도 후보 제품이 검색됐으면 추천을 생성한다 —
    프롬프트가 "없는 근거 인용 금지"를 강제하므로 개인화 근거 없이도 안전하다. 예전에는
    graph_paths·memory_facts 가 비면 무조건 폴백해서, 신규 사용자는 아무리 구체적으로
    요청해도 추천을 받을 수 없었다.
    """
    if not context.graph_paths and not context.memory_facts and not context.products:
        logger.info("rationale_no_grounding", query=context.query)
        return Rationale(response=FALLBACK_MESSAGE)

    prompt = _format_context(context)
    for attempt in (1, 2):
        try:
            raw = provider.complete_json(_PROMPT, prompt, _SCHEMA)
            rationale = Rationale.model_validate(raw)
            return _drop_uncited_out_of_range(rationale, context)
        except (LLMError, ValidationError) as exc:
            logger.warning("rationale_retry", attempt=attempt, error=str(exc))
    logger.error("rationale_gave_up", query=context.query)
    return Rationale(response=FALLBACK_MESSAGE)


def _drop_uncited_out_of_range(rationale: Rationale, context: RetrievalContext) -> Rationale:
    """LLM 이 지어낸(범위 밖) 인용은 드롭 — 환각 방지(AC-R3)."""
    valid_paths = [
        i for i in rationale.cited_graph_path_indices if 0 <= i < len(context.graph_paths)
    ]
    valid_memory_ids = {m.memory_id for m in context.memory_facts}
    valid_memories = [mid for mid in rationale.cited_memory_ids if mid in valid_memory_ids]
    return rationale.model_copy(
        update={"cited_graph_path_indices": valid_paths, "cited_memory_ids": valid_memories}
    )


def _format_path(index: int, path: GraphPath) -> str:
    chain = " -> ".join(n.label or n.key for n in path.nodes)
    return f"- 인덱스 {index}: {chain}"


def _format_context(context: RetrievalContext) -> str:
    lines = [f"질문: {context.query}", "", "[후보 제품]"]
    for p in context.products:
        line = (
            f"- product_id={p.product_id} {p.name} ({p.brand or '브랜드미상'}"
            f"{f', 카테고리: {p.category}' if p.category else ''})"
        )
        if p.description:
            line += f" — {p.description}"
        if p.ingredients:
            line += f" [성분: {', '.join(p.ingredients)}]"
        lines.append(line)
    lines += ["", "[그래프 경로]"]
    lines += [_format_path(i, path) for i, path in enumerate(context.graph_paths)]
    lines += ["", "[회상된 개인 기억]"]
    lines += [
        f"- memory_id={f.memory_id} ({f.fact_type}): {f.content}" for f in context.memory_facts
    ]
    lines += ["", "[참고 문서]"]
    lines += [f"- doc_id={d.doc_id}: {d.content}" for d in context.doc_hits]
    return "\n".join(lines)
