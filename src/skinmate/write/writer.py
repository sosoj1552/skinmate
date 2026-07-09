"""writer.py(⭐4) — 단일 트랜잭션 원자 저장. 표(memories+감사)+그래프(개인 엣지)를 한 커넥션·

한 Postgres 트랜잭션에 묶어, 실패 시 전부 롤백한다(AC-S1). AGE 는 같은 Postgres 트랜잭션
위에서 동작하므로 별도 2PC 없이 `db.user_scope` 하나로 원자성이 보장된다. 턴이 반환되기
전에 커밋되어 다음 턴에서 즉시 보인다(AC-S2). per-user advisory lock 으로 동시 쓰기를
직렬화한다(F5). 담당 B.
"""

from __future__ import annotations

from typing import Any

import psycopg
import structlog

from skinmate import db
from skinmate.contracts.facts import FactType
from skinmate.llm.base import LLMProvider
from skinmate.memory import bridge, crud, repo
from skinmate.memory.crud import CrudDecision
from skinmate.memory.extract import extract_facts

logger = structlog.get_logger(__name__)


def write_turn(
    conn: psycopg.Connection[Any],
    provider: LLMProvider,
    user_id: int,
    utterance: str,
) -> list[CrudDecision]:
    """한 턴의 사용자 발화를 추출→판정→원자 저장한다. 반환: 이 턴에 반영된 판정 목록.

    호출자가 연 커넥션을 그대로 쓴다(풀링·수명은 호출자 책임). 내부적으로 트랜잭션을 열고
    advisory lock 을 건 뒤, 표 쓰기(crud)와 그래프 쓰기(bridge)를 같은 트랜잭션에 순서대로
    수행한다 — 어느 한쪽이라도 실패하면 전체 롤백(AC-S1). fact 가 없으면(잡담) 아무 것도
    쓰지 않고 빈 목록을 반환한다.
    """
    facts = extract_facts(provider, utterance)
    if not facts:
        return []

    decisions: list[CrudDecision] = []
    with db.user_scope(conn, user_id):
        db.advisory_xact_lock(conn, user_id)
        for fact in facts:
            existing = repo.list_active(conn, user_id)
            decision = crud.judge(existing, fact)

            ingredient_id: int | None = None
            ingredient_key: str | None = None
            if fact.fact_type in bridge.INGREDIENT_FACT_TYPES and fact.target_name:
                resolved = bridge.resolve_ingredient(conn, fact.target_name)
                if resolved is None:
                    logger.warning("writer_ingredient_unresolved", target_name=fact.target_name)
                else:
                    ingredient_id, ingredient_key = resolved

            concern_key: str | None = None
            if fact.fact_type == FactType.HAS_CONCERN and fact.target_name:
                concern_key = bridge.resolve_concern_key(conn, fact.target_name)
                if concern_key is None:
                    logger.warning("writer_concern_unresolved", target_name=fact.target_name)

            crud.apply_decision(conn, user_id, decision, target_ingredient_id=ingredient_id)
            bridge.project_to_graph(
                conn, user_id, decision, ingredient_key=ingredient_key, concern_key=concern_key
            )
            decisions.append(decision)

    return decisions
