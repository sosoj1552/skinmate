"""process_turn(2.1) 통합 배선 테스트 — 대화↔실물검색↔저장이 한 턴 안에서 맞물리는지 검증.

PRD §1 read→respond→write 골격을 실제로 태운다: fixture 대신 retrieve_recommendation_context
(1A.7)로 검색하고, 응답 후 write_turn(1B.4)으로 원자 저장한다. 대표 시나리오(PRD §8, AC-R4)의
4개 단언과 AC-M4(기억 유무에 따른 추천 차이)를 함께 검증한다.
"""

from __future__ import annotations

from typing import Any

import psycopg

from skinmate.app.turn import process_turn
from skinmate.chat.route import Route
from skinmate.contracts.facts import FactType
from skinmate.documents.embed import embed_text
from skinmate.graph.knowledge_populate import populate_global_knowledge
from skinmate.memory import bridge, crud
from skinmate.memory.crud import CrudDecision, CrudOp
from skinmate.memory.extract import ExtractedFact
from skinmate.retrieval.retrieve import retrieve_recommendation_context

_UID = 1


class _ScriptedProvider:
    """호출마다 미리 정한 payload 를 순서대로 반환(라우팅→근거생성→fact추출 3단계)."""

    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self._payloads = list(payloads)

    def complete(self, system: str, prompt: str) -> str:
        return ""

    def complete_json(
        self, system: str, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        return self._payloads.pop(0)


def _seed_scenario(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """PRD §8 대표 시나리오 픽스처: 성분·제품·문서·전역그래프 + 기존 기억(오일 회피/가을 건조)."""
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
            ('test_hyaluronic_acid', '테스트히알루론산',
             '피부에 강력한 수분을 공급하여 건조함을 예방하고 보습력을 올립니다.'),
            ('test_ethanol', '테스트에탄올', '피부에 강한 자극을 줍니다.')
            ON CONFLICT (canonical_key) DO UPDATE
            SET name_ko = EXCLUDED.name_ko, intro = EXCLUDED.intro
            RETURNING canonical_key, ingredient_id;
            """)
        ing_map = {row[0]: row[1] for row in cur.fetchall()}
        hyaluronic_id = ing_map["test_hyaluronic_acid"]
        ethanol_id = ing_map["test_ethanol"]

        # 동일 베이스 벡터 — soft-ranking/hard-filter 효과만 격리 검증
        emb_same = embed_text("건조함을 완화해주는 촉촉한 피부 화장품")
        cur.execute(
            """
            INSERT INTO products (name, brand, description, embedding, embedding_model_id)
            VALUES
            ('테스트 수분 에멀전', '브랜드A', '끈적임 없는 에멀전 제형의 고보습 제품',
             %s, 'bge-m3'),
            ('테스트 페이스 오일', '브랜드B', '오일 제형의 고보습 제품', %s, 'bge-m3'),
            ('테스트 자극 크림', '브랜드C', '보습 크림', %s, 'bge-m3')
            RETURNING name, product_id;
            """,
            (emb_same, emb_same, emb_same),
        )
        prod_map = {row[0]: row[1] for row in cur.fetchall()}
        emulsion_id = prod_map["테스트 수분 에멀전"]
        oil_id = prod_map["테스트 페이스 오일"]
        irritant_cream_id = prod_map["테스트 자극 크림"]

        cur.execute(f"""
            INSERT INTO product_ingredients (product_id, ingredient_id) VALUES
            ({emulsion_id}, {hyaluronic_id}),
            ({oil_id}, {hyaluronic_id}),
            ({irritant_cream_id}, {ethanol_id});
            """)

    # 전역 지식 그래프 투영(멱등)
    populate_global_knowledge(conn)

    # 기존 기억: 가을철 건조 고민 + 오일 텍스처 회피 + 에탄올(자극) 성분 회피
    concern = ExtractedFact(
        fact_type=FactType.HAS_CONCERN, content="가을철 건조", target_name="건조", season="가을"
    )
    d_concern = CrudDecision(op=CrudOp.ADD, fact=concern)
    crud.apply_decision(conn, _UID, d_concern)
    bridge.project_to_graph(conn, _UID, d_concern, concern_key="dryness")

    oil_texture = ExtractedFact(fact_type=FactType.OTHER, content="오일 제형은 안 맞아서 회피함")
    d_oil = CrudDecision(op=CrudOp.ADD, fact=oil_texture)
    oil_memory_id = crud.apply_decision(conn, _UID, d_oil)

    avoid_ethanol = ExtractedFact(
        fact_type=FactType.AVOID_INGREDIENT, content="에탄올 자극나요", target_name="테스트에탄올"
    )
    d_avoid = CrudDecision(op=CrudOp.ADD, fact=avoid_ethanol)
    crud.apply_decision(conn, _UID, d_avoid, target_ingredient_id=ethanol_id)
    bridge.project_to_graph(conn, _UID, d_avoid, ingredient_key="test_ethanol")

    return {
        "emulsion_id": emulsion_id,
        "oil_id": oil_id,
        "irritant_cream_id": irritant_cream_id,
        "oil_memory_id": oil_memory_id,
    }


_UTTERANCE = "가을이 되니 건조해요. 끈적한 제형은 싫어서 오일 말고, 에멀전인데 보습 확실한 거요."


def test_representative_scenario_four_assertions(db_conn: psycopg.Connection) -> None:
    """PRD §8 대표 시나리오 4단언(AC-R4)을 실제 검색 융합 결과로 검증."""
    ids = _seed_scenario(db_conn)

    context = retrieve_recommendation_context(
        db_conn, user_id=_UID, query=_UTTERANCE, season="가을", limit=50
    )

    # (a) 고민→성분 경로(계절 반영) 포함
    concern_paths = [p for p in context.graph_paths if any(n.key == "dryness" for n in p.nodes)]
    assert concern_paths, "건조 고민 경로가 근거에 없음"
    seasoned = [e for p in concern_paths for e in p.edges if e.season == "가을"]
    assert seasoned, "계절(가을) 정보가 경로 엣지에 반영되지 않음"

    # (b) 회피 성분(에탄올) 포함 제품 0건
    product_ids = {p.product_id for p in context.products}
    assert ids["irritant_cream_id"] not in product_ids

    # (c) 에멀전 제품이 동일 고민 매칭 오일 제품보다 상위
    ranked_ids = [p.product_id for p in context.products]
    assert ids["emulsion_id"] in ranked_ids and ids["oil_id"] in ranked_ids
    assert ranked_ids.index(ids["emulsion_id"]) < ranked_ids.index(ids["oil_id"])

    # (d) 회상된 기억("오일 회피") 등장
    recalled_ids = {f.memory_id for f in context.memory_facts}
    assert ids["oil_memory_id"] in recalled_ids


def test_process_turn_wires_real_retrieval_and_persists_fact(db_conn: psycopg.Connection) -> None:
    """2.1: process_turn 이 fixture 가 아닌 실물 검색을 쓰고, 응답 후 새 사실을 원자 저장한다."""
    ids = _seed_scenario(db_conn)

    provider = _ScriptedProvider(
        [
            {"intent": "recommendation", "has_texture_slot": True, "has_concern_slot": True},
            {
                "response": "건조 고민에 맞는 수분 에멀전을 추천해요. 오일 회피 기억도 반영했어요.",
                "cited_graph_path_indices": [0],
                "cited_memory_ids": [ids["oil_memory_id"]],
            },
            {
                "facts": [
                    {
                        "fact_type": "avoid_ingredient",
                        "content": "레티놀도 자극나요",
                        "target_name": "레티놀",
                    }
                ]
            },
        ]
    )

    result = process_turn(db_conn, provider, _UID, _UTTERANCE, season="가을")

    assert result.route == Route.SPECIFIC
    assert ids["oil_memory_id"] in result.cited_memory_ids  # (d) 실물 회상이 응답에 반영

    # 응답 후 write_turn 이 같은 utterance 에서 새 사실을 원자 저장했는지 확인
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT content FROM memories "
            "WHERE user_id=%s AND target_name=%s AND deleted_at IS NULL",
            (_UID, "레티놀"),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == "레티놀도 자극나요"


def test_process_turn_ac_m4_memory_changes_recommendation(db_conn: psycopg.Connection) -> None:
    """AC-M4: 기억 有/無 사용자의 추천이 실질적으로 다르다(회피성분 반영 여부)."""
    ids = _seed_scenario(db_conn)
    other_uid = 2  # 동일 시나리오 아래, 기억이 없는 사용자

    ctx_with = retrieve_recommendation_context(
        db_conn, user_id=_UID, query=_UTTERANCE, season="가을", limit=50
    )
    ctx_without = retrieve_recommendation_context(
        db_conn, user_id=other_uid, query=_UTTERANCE, season="가을", limit=50
    )

    with_ids = {p.product_id for p in ctx_with.products}
    without_ids = {p.product_id for p in ctx_without.products}

    # 기억 없는 사용자는 회피성분 제품도 그대로 노출 — 두 결과 집합이 달라야 함(AC-M4)
    assert ids["irritant_cream_id"] not in with_ids
    assert ids["irritant_cream_id"] in without_ids
    assert with_ids != without_ids
