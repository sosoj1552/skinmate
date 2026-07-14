"""턴 배선(2.1) — fixture 대신 실물 검색(1A.7)·저장(1B.4)을 대화 총괄(1B.6)에 묶는다.

PRD §1 공통 런타임 흐름을 그대로 구현한다: 라우팅 → [SPECIFIC 이면] 실물 검색 융합 →
근거 생성/응답 → 응답 반환 후 fact 추출·CRUD 판정·원자 저장. 검색·기억조회는 응답 생성에
쓰인 시점의 기억 상태를 반영해야 하므로 반드시 write_turn 이전에 수행한다(AC-M6 최근성
루프가 "다음 턴"부터 반영됨을 보장하는 전제).
"""

from __future__ import annotations

from typing import Any

import psycopg

from skinmate import db
from skinmate.chat.funnel import is_funnel_question
from skinmate.chat.orchestrator import TurnResult, handle_turn
from skinmate.chat.route import Route, RouteDecision, classify_route
from skinmate.llm.base import LLMProvider
from skinmate.memory.rank import rank_memory
from skinmate.retrieval.retrieve import retrieve_recommendation_context
from skinmate.write.writer import write_turn


def process_turn(
    conn: psycopg.Connection[Any],
    provider: LLMProvider,
    user_id: int,
    utterance: str,
    *,
    history: list[str] | None = None,
    season: str | None = None,
) -> TurnResult:
    """한 턴 전체(읽기→응답→쓰기)를 처리해 TurnResult 를 반환한다.

    호출자가 연 커넥션을 그대로 쓴다(수명·풀링은 호출자 책임, write_turn 과 동일 관례).
    """
    decision = classify_route(provider, utterance, history=history)

    with db.user_scope(conn, user_id):
        if decision.route == Route.SPECIFIC:
            retrieval_context = retrieve_recommendation_context(
                conn, user_id, utterance, season=season
            )
            memory_facts = retrieval_context.memory_facts
        else:
            retrieval_context = None
            memory_facts = rank_memory(conn, user_id)

    result = handle_turn(
        provider,
        utterance,
        history=history,
        memory_facts=memory_facts,
        retrieval_context=retrieval_context,
        route_decision=decision,
    )

    write_turn(conn, provider, user_id, utterance)

    # 퍼널 후속 자동 재추천: 직전 봇 메시지가 퍼널 질문이고 이번 발화가 정보 진술이면,
    # 사용자는 좁히기 질문에 답한 것이다 — "기억해 둘게요"로 끝내지 않고, 방금 저장된
    # 기억까지 반영해 같은 턴에서 원 요청(history[-2])에 대한 추천으로 이어간다.
    # (write_turn 뒤에 다시 검색하는 유일한 예외 경로 — 최근성 루프의 "다음 턴 반영"
    # 원칙은 유지되고, 여기서는 의도적으로 이번 답변을 즉시 반영한다.)
    if (
        decision.route == Route.STATEMENT
        and history is not None
        and len(history) >= 2
        and is_funnel_question(history[-1])
    ):
        followup_query = f"{history[-2]} {utterance}"
        with db.user_scope(conn, user_id):
            followup_context = retrieve_recommendation_context(
                conn, user_id, followup_query, season=season
            )
        return handle_turn(
            provider,
            followup_query,
            history=history,
            memory_facts=followup_context.memory_facts,
            retrieval_context=followup_context,
            route_decision=RouteDecision(route=Route.SPECIFIC),
        )

    return result
