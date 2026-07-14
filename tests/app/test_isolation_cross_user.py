"""격리 테스트(WBS 2.3, AC-M5/G3) — 사용자 2명 시드, 서로의 기억·그래프를 정확히 0행 격리.

ACCEPTANCE-TESTING.md §3 P0 필수 테스트 목록의 test_isolation_cross_user.py 에 대응한다.
RLS(관계형 memories)와 choke(그래프) 양쪽 경로를 모두 검증하고, 개별 원시함수(rank_memory,
choke.age_exec)뿐 아니라 검색 융합 창구(retrieve_recommendation_context)를 통과할 때도
격리가 유지되는지까지 확인한다 — 1A/1B 각자 테스트가 이미 커버한 것을, 2.1 통합 배선 위에서
다시 한 번 교차 확인하는 것이 이 테스트의 목적.
"""

from __future__ import annotations

from typing import Any

import psycopg

from skinmate.contracts.facts import FactType
from skinmate.contracts.graph import NodeKind
from skinmate.graph import choke
from skinmate.graph.knowledge_populate import populate_global_knowledge
from skinmate.memory import bridge, crud
from skinmate.memory.crud import CrudDecision, CrudOp
from skinmate.memory.extract import ExtractedFact
from skinmate.memory.rank import rank_memory
from skinmate.retrieval.retrieve import retrieve_recommendation_context

_UID_A = 1
_UID_B = 2


def _seed_two_users(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """A/B 두 사용자가 서로 다른 성분·고민을 기억/그래프에 갖도록 시드한다."""
    with conn.cursor() as cur:
        cur.execute("SET LOCAL search_path = ag_catalog, public;")
        cur.execute("""
        DO $$
        DECLARE
            g name := 'skinmate';
            gid oid;
            lbl text;
            vlabels text[] := ARRAY['User','Ingredient','Product','Concern','Brand'];
            elabels text[] := ARRAY['CONTAINS','TREATS','AGGRAVATES','HELPS',
                                     'CONFLICTS','HAS_CONCERN','AVOIDS','PREFERS'];
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = g) THEN
                PERFORM ag_catalog.create_graph(g);
            END IF;
            SELECT graphid INTO gid FROM ag_catalog.ag_graph WHERE name = g;
            FOREACH lbl IN ARRAY vlabels LOOP
                IF NOT EXISTS (
                    SELECT 1 FROM ag_catalog.ag_label WHERE name = lbl AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_vlabel(g, lbl);
                END IF;
            END LOOP;
            FOREACH lbl IN ARRAY elabels LOOP
                IF NOT EXISTS (
                    SELECT 1 FROM ag_catalog.ag_label WHERE name = lbl AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_elabel(g, lbl);
                END IF;
            END LOOP;
        END $$;
        """)

        cur.execute("SET LOCAL search_path = public;")
        cur.execute("""
            INSERT INTO ingredients (canonical_key, name_ko, intro)
            VALUES
            ('test_iso_retinol', '테스트격리레티놀', '자극을 줄 수 있는 항노화 성분.'),
            ('test_iso_centella', '테스트격리센텔라', '진정에 도움을 주는 성분.')
            ON CONFLICT (canonical_key) DO UPDATE
            SET name_ko = EXCLUDED.name_ko, intro = EXCLUDED.intro
            RETURNING canonical_key, ingredient_id;
            """)
        ing_map = {row[0]: row[1] for row in cur.fetchall()}
        retinol_id = ing_map["test_iso_retinol"]
        centella_id = ing_map["test_iso_centella"]

    populate_global_knowledge(conn)

    # A: 레티놀 회피 + 건조 고민
    fact_a_avoid = ExtractedFact(
        fact_type=FactType.AVOID_INGREDIENT,
        content="레티놀 자극나요",
        target_name="테스트격리레티놀",
    )
    d_a_avoid = CrudDecision(op=CrudOp.ADD, fact=fact_a_avoid)
    crud.apply_decision(conn, _UID_A, d_a_avoid, target_ingredient_id=retinol_id)
    bridge.project_to_graph(conn, _UID_A, d_a_avoid, ingredient_key="test_iso_retinol")

    fact_a_concern = ExtractedFact(
        fact_type=FactType.HAS_CONCERN, content="건조해요", target_name="건조"
    )
    d_a_concern = CrudDecision(op=CrudOp.ADD, fact=fact_a_concern)
    crud.apply_decision(conn, _UID_A, d_a_concern)
    bridge.project_to_graph(conn, _UID_A, d_a_concern, concern_key="dryness")

    # B: 센텔라 선호 + 민감 고민(A와 겹치지 않는 별개의 사실)
    fact_b_prefer = ExtractedFact(
        fact_type=FactType.PREFER_INGREDIENT,
        content="센텔라 진정돼서 좋아요",
        target_name="테스트격리센텔라",
    )
    d_b_prefer = CrudDecision(op=CrudOp.ADD, fact=fact_b_prefer)
    crud.apply_decision(conn, _UID_B, d_b_prefer, target_ingredient_id=centella_id)
    bridge.project_to_graph(conn, _UID_B, d_b_prefer, ingredient_key="test_iso_centella")

    fact_b_concern = ExtractedFact(
        fact_type=FactType.HAS_CONCERN, content="민감해요", target_name="민감"
    )
    d_b_concern = CrudDecision(op=CrudOp.ADD, fact=fact_b_concern)
    crud.apply_decision(conn, _UID_B, d_b_concern)
    bridge.project_to_graph(conn, _UID_B, d_b_concern, concern_key="sensitivity")

    return {"retinol_id": retinol_id, "centella_id": centella_id}


def test_memories_isolation_rank_memory(db_conn: psycopg.Connection) -> None:
    """RLS: rank_memory(A) 에 B 사실이 정확히 0건, 반대도 마찬가지(AC-M5)."""
    _seed_two_users(db_conn)

    ranked_a = rank_memory(db_conn, _UID_A)
    ranked_b = rank_memory(db_conn, _UID_B)

    a_contents = {f.content for f in ranked_a}
    b_contents = {f.content for f in ranked_b}

    assert "센텔라 진정돼서 좋아요" not in a_contents
    assert "민감해요" not in a_contents
    assert "레티놀 자극나요" not in b_contents
    assert "건조해요" not in b_contents
    # 상호 배제이지 전멸이 아님을 함께 확인(비파괴)
    assert "레티놀 자극나요" in a_contents
    assert "센텔라 진정돼서 좋아요" in b_contents


def test_graph_isolation_choke_cross_query(db_conn: psycopg.Connection) -> None:
    """choke: A 스코프로 B 의 개인 그래프 엣지를 조회하면 정확히 0행(AC-G3)."""
    _seed_two_users(db_conn)

    # A 스코프로 B 전용 엣지(PREFERS 센텔라)를 찾으려 하면 0행이어야 한다
    rows_a_probing_b = choke.age_exec(
        db_conn,
        _UID_A,
        "MATCH (u:User {user_id: $user_scope})-[:PREFERS]->"
        "(i:Ingredient {canonical_key: 'test_iso_centella'}) RETURN i.canonical_key AS ck",
        {},
    )
    assert rows_a_probing_b == []

    # B 스코프로 A 전용 엣지(AVOIDS 레티놀)를 찾으려 하면 0행이어야 한다
    rows_b_probing_a = choke.age_exec(
        db_conn,
        _UID_B,
        "MATCH (u:User {user_id: $user_scope})-[:AVOIDS]->"
        "(i:Ingredient {canonical_key: 'test_iso_retinol'}) RETURN i.canonical_key AS ck",
        {},
    )
    assert rows_b_probing_a == []

    # 본인 스코프로는 정상 조회되어야 함(격리가 과잉 차단이 아님을 함께 확인)
    rows_a_own = choke.age_exec(
        db_conn,
        _UID_A,
        "MATCH (u:User {user_id: $user_scope})-[:AVOIDS]->"
        "(i:Ingredient {canonical_key: 'test_iso_retinol'}) RETURN i.canonical_key AS ck",
        {},
    )
    assert len(rows_a_own) == 1


def test_retrieve_recommendation_context_isolation(db_conn: psycopg.Connection) -> None:
    """통합 창구(1A.7 retrieve_recommendation_context)를 통과해도 격리가 유지된다."""
    _seed_two_users(db_conn)

    ctx_a = retrieve_recommendation_context(
        db_conn, user_id=_UID_A, query="피부 진정 성분 추천", limit=50
    )
    ctx_b = retrieve_recommendation_context(
        db_conn, user_id=_UID_B, query="피부 진정 성분 추천", limit=50
    )

    a_memory_contents = {f.content for f in ctx_a.memory_facts}
    b_memory_contents = {f.content for f in ctx_b.memory_facts}
    assert "센텔라 진정돼서 좋아요" not in a_memory_contents
    assert "레티놀 자극나요" not in b_memory_contents

    a_graph_users = {n.key for p in ctx_a.graph_paths for n in p.nodes if n.kind == NodeKind.USER}
    b_graph_users = {n.key for p in ctx_b.graph_paths for n in p.nodes if n.kind == NodeKind.USER}
    assert str(_UID_B) not in a_graph_users
    assert str(_UID_A) not in b_graph_users


def test_write_turn_isolation_via_process_turn(db_conn: psycopg.Connection) -> None:
    """process_turn(2.1 배선) 으로 A가 쓴 사실이 B의 조회에 정확히 0행(AC-M5, 통합 레벨)."""
    from skinmate.app.turn import process_turn

    class _ScriptedProvider:
        def __init__(self, payloads: list[dict[str, object]]) -> None:
            self._payloads = list(payloads)

        def complete(self, system: str, prompt: str) -> str:
            return ""

        def complete_json(
            self, system: str, prompt: str, schema: dict[str, object]
        ) -> dict[str, object]:
            return self._payloads.pop(0)

    provider = _ScriptedProvider(
        [
            {"intent": "statement"},
            {
                "facts": [
                    {
                        "fact_type": "avoid_ingredient",
                        "content": "니아신아마이드 트러블나요",
                        "target_name": "니아신아마이드",
                    }
                ]
            },
        ]
    )
    process_turn(db_conn, provider, _UID_A, "니아신아마이드 쓰면 트러블나요")

    ranked_b = rank_memory(db_conn, _UID_B)
    assert "니아신아마이드 트러블나요" not in {f.content for f in ranked_b}
