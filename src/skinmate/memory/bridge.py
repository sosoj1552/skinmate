"""기억→그래프 다리(1B.5) — 개인 회피/선호/고민 연결선을 choke 경유로 투영한다.

writer(1B.4)의 같은 트랜잭션 안에서 호출된다(PRD F4-bridge, ⭐5). 성분은 관계형 ingredients
로 canonical_key 를 해석해 A 의 전역 Ingredient 노드(canonical_key 키)에 MERGE 하고, 고민은
그래프에 이미 시드된 Concern.label 로 canonical name 을 역조회한다 — 둘 다 A 의 전역 지식과
같은 노드를 공유해야 2-hop 순회가 성립한다(AC-G2). 해석 실패 시 그래프 엣지만 skip
(PRD F1 예외) — 텍스트 사실은 이미 memories 에 보존된 뒤다. 브랜드는 전역 차원 테이블이
없어 target_name 을 그대로 Brand.name 키로 쓴다.
"""

from __future__ import annotations

from typing import Any

import psycopg
import structlog

from skinmate.contracts.facts import FactType
from skinmate.graph import choke
from skinmate.memory.crud import CrudDecision, CrudOp

logger = structlog.get_logger(__name__)

INGREDIENT_FACT_TYPES = frozenset({FactType.AVOID_INGREDIENT, FactType.PREFER_INGREDIENT})
BRAND_FACT_TYPES = frozenset({FactType.AVOID_BRAND, FactType.PREFER_BRAND})

_INGREDIENT_EDGE = {FactType.AVOID_INGREDIENT: "AVOIDS", FactType.PREFER_INGREDIENT: "PREFERS"}
_BRAND_EDGE = {FactType.AVOID_BRAND: "AVOIDS", FactType.PREFER_BRAND: "PREFERS"}


def resolve_ingredient(conn: psycopg.Connection[Any], name: str) -> tuple[int, str] | None:
    """자유텍스트 성분명 → (ingredient_id, canonical_key). 매칭 실패 시 None(PRD F1 예외)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ingredient_id, canonical_key FROM ingredients
            WHERE lower(name_ko) = lower(%s) OR lower(name_en) = lower(%s)
               OR lower(canonical_key) = lower(%s)
            LIMIT 1
            """,
            (name, name, name),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else None


def resolve_concern_key(conn: psycopg.Connection[Any], label: str) -> str | None:
    """자유텍스트 고민 라벨(예: '건조') → 그래프에 시드된 Concern.name(예: 'dryness').

    A 의 전역 지식(knowledge_populate.py)이 심어둔 Concern 노드만 해석 가능 — 미시드 고민은
    None(그래프 엣지 skip, 사실 자체는 memories 에 원문 그대로 보존됨).
    """
    rows = choke.age_exec(
        conn,
        None,
        "MATCH (c:Concern) WHERE c.label = $label RETURN c.name AS name",
        {"label": label},
    )
    # 단일 스칼라 컬럼 조회는 [{"name":...}] 가 아니라 ["dryness"] 형태(순수값 리스트)로 온다.
    return str(rows[0]) if rows else None


def _edge_type_for(
    decision: CrudDecision, edge_map: dict[FactType, str], *, use_existing: bool
) -> str | None:
    fact_type = (
        decision.existing.fact_type
        if use_existing and decision.existing is not None
        else decision.fact.fact_type
    )
    return edge_map.get(fact_type)


def project_to_graph(
    conn: psycopg.Connection[Any],
    user_id: int,
    decision: CrudDecision,
    *,
    ingredient_key: str | None = None,
    concern_key: str | None = None,
) -> None:
    """CRUD 판정을 그래프 개인 엣지에 반영. no-op 판정은 아무것도 하지 않는다.

    writer 가 이미 연 트랜잭션 안에서 호출한다 — 실패 시 writer 의 롤백에 자연히 포함된다
    (choke 는 같은 Postgres 트랜잭션 위에서 동작하므로 별도 보상 로직이 필요 없다, AC-S1).
    """
    fact = decision.fact
    if decision.op == CrudOp.NOOP:
        return

    if fact.fact_type in INGREDIENT_FACT_TYPES:
        _project_labeled(
            conn, user_id, decision, "Ingredient", "canonical_key", ingredient_key, _INGREDIENT_EDGE
        )
    elif fact.fact_type in BRAND_FACT_TYPES:
        _project_labeled(conn, user_id, decision, "Brand", "name", fact.target_name, _BRAND_EDGE)
    elif fact.fact_type == FactType.HAS_CONCERN:
        _project_concern(conn, user_id, decision, concern_key)
    # skin_type · other 는 그래프 투영 없음(관계형만, migration 002 주석)


def _project_labeled(
    conn: psycopg.Connection[Any],
    user_id: int,
    decision: CrudDecision,
    label: str,
    key_prop: str,
    key_value: str | None,
    edge_map: dict[FactType, str],
) -> None:
    if key_value is None:
        logger.warning("bridge_skip_unresolved", label=label, memory_id=decision.target_memory_id)
        return

    if decision.op in (CrudOp.UPDATE, CrudOp.DELETE):
        old_edge = _edge_type_for(decision, edge_map, use_existing=True)
        if old_edge is not None:
            choke.age_exec(
                conn,
                user_id,
                f"MATCH (u:User {{user_id: $user_scope}})-[r:{old_edge}]->"
                f"(t:{label} {{{key_prop}: $key}}) DELETE r",
                {"key": key_value},
            )

    if decision.op in (CrudOp.ADD, CrudOp.UPDATE):
        new_edge = _edge_type_for(decision, edge_map, use_existing=False)
        assert new_edge is not None  # ADD/UPDATE 는 fact.fact_type 이 항상 edge_map 에 있음
        choke.age_exec(
            conn,
            user_id,
            f"MERGE (u:User {{user_id: $user_scope}}) "
            f"MERGE (t:{label} {{{key_prop}: $key}}) "
            f"MERGE (u)-[r:{new_edge}]->(t) SET r.user_scope = $user_scope",
            {"key": key_value},
        )


def _project_concern(
    conn: psycopg.Connection[Any], user_id: int, decision: CrudDecision, concern_key: str | None
) -> None:
    if concern_key is None:
        logger.warning(
            "bridge_skip_unresolved", label="Concern", memory_id=decision.target_memory_id
        )
        return

    if decision.op == CrudOp.DELETE:
        choke.age_exec(
            conn,
            user_id,
            "MATCH (u:User {user_id: $user_scope})-[r:HAS_CONCERN]->"
            "(c:Concern {name: $key}) DELETE r",
            {"key": concern_key},
        )
        return

    choke.age_exec(
        conn,
        user_id,
        "MERGE (u:User {user_id: $user_scope}) "
        "MERGE (c:Concern {name: $key}) "
        "MERGE (u)-[r:HAS_CONCERN]->(c) SET r.user_scope = $user_scope, r.season = $season",
        {"key": concern_key, "season": decision.fact.season},
    )
