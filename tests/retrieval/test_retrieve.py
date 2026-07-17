"""WBS 1A.7 검색 3종 합치기 및 제형 soft-ranking 단위 테스트.

그래프 융합은 GraphRAG 개념 기반 메타패스(W1/W2, graph/traverse.py)로 전환되었다 — 질의에서
인식한 고민에 대해 추천 성분을 순회하고, 그 성분을 함유한 제품을 product_ingredients로
JOIN한다(회피 성분 하드필터는 그대로 유지). 그래프는 ingredient_id 키의 Ingredient 노드·
Mechanism 노드·ACHIEVES/TREATS(+source_doc_ids) 표현을 쓴다.
"""

from __future__ import annotations

import psycopg

from skinmate import db
from skinmate.contracts.facts import FactType
from skinmate.documents.embed import embed_text
from skinmate.graph import choke
from skinmate.memory import crud
from skinmate.memory.crud import CrudDecision, CrudOp
from skinmate.memory.extract import ExtractedFact
from skinmate.retrieval.retrieve import retrieve_recommendation_context


def _ensure_graph_labels(cur: psycopg.Cursor) -> None:
    cur.execute("SET LOCAL search_path = ag_catalog, public;")
    cur.execute("""
    DO $$
    DECLARE
        g name := 'skinmate';
        gid oid;
        lbl text;
        vlabels text[] := ARRAY['User','Ingredient','Product','Concern','Brand','Mechanism'];
        elabels text[] := ARRAY['CONTAINS','TREATS','AGGRAVATES','HELPS',
                                 'CONFLICTS','HAS_CONCERN','AVOIDS','PREFERS',
                                 'ACHIEVES','ENABLES'];
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


def _seed_w1_dryness(conn: psycopg.Connection, hyaluronic_id: int, doc_id: int) -> None:
    """히알루론산 -[ACHIEVES]-> 보습 -[TREATS]-> dryness (W1) 를 근거문서(doc_id)와 함께
    적재한다(graphrag_slice.py 패턴: 노드는 각각 MERGE, 관계 속성은 별도 MATCH+SET)."""
    choke.age_exec(
        conn,
        None,
        "MERGE (i:Ingredient {ingredient_id: $id}) SET i.name_ko = $name",
        {"id": hyaluronic_id, "name": "테스트히알루론산"},
    )
    choke.age_exec(conn, None, "MERGE (m:Mechanism {name: $name})", {"name": "보습"})
    choke.age_exec(
        conn,
        None,
        "MERGE (c:Concern {name: $name}) SET c.label = $label",
        {"name": "dryness", "label": "건조"},
    )
    choke.age_exec(
        conn,
        None,
        "MERGE (i:Ingredient {ingredient_id: $id}) MERGE (m:Mechanism {name: $mech}) "
        "MERGE (i)-[:ACHIEVES]->(m)",
        {"id": hyaluronic_id, "mech": "보습"},
    )
    choke.age_exec(
        conn,
        None,
        "MATCH (i:Ingredient {ingredient_id: $id})-[r:ACHIEVES]->(m:Mechanism {name: $mech}) "
        "SET r.source_doc_ids = $docs, r.origin = 'manual', r.confidence = 1.0",
        {"id": hyaluronic_id, "mech": "보습", "docs": [doc_id]},
    )
    choke.age_exec(
        conn,
        None,
        "MERGE (m:Mechanism {name: $mech}) MERGE (c:Concern {name: $concern}) "
        "MERGE (m)-[:TREATS]->(c)",
        {"mech": "보습", "concern": "dryness"},
    )
    choke.age_exec(
        conn,
        None,
        "MATCH (m:Mechanism {name: $mech})-[r:TREATS]->(c:Concern {name: $concern}) "
        "SET r.source_doc_ids = $docs, r.origin = 'manual', r.confidence = 1.0",
        {"mech": "보습", "concern": "dryness", "docs": [doc_id]},
    )


def test_retrieve_recommendation_context_integration(db_conn: psycopg.Connection) -> None:
    """RAG, 그래프(W1/W2), 기억이 융합된 RetrievalContext 및 제형 soft-ranking 작동을 검증합니다."""
    with db_conn.cursor() as cur:
        _ensure_graph_labels(cur)

        # 2. 테스트용 RDB 데이터 삽입 (임베딩 포함)
        cur.execute("SET LOCAL search_path = public;")

        # 성분 삽입
        cur.execute("""
            INSERT INTO ingredients (canonical_key, name_ko, intro)
            VALUES
            (
                'test_hyaluronic_acid',
                '테스트히알루론산',
                '피부에 강력한 수분을 공급하여 건조함을 예방하고 보습력을 올립니다.'
            ),
            (
                'test_ethanol',
                '테스트에탄올',
                '피부에 강한 자극을 줍니다.'
            )
            ON CONFLICT (canonical_key) DO UPDATE
            SET name_ko = EXCLUDED.name_ko, intro = EXCLUDED.intro
            RETURNING canonical_key, ingredient_id;
            """)
        ing_map = {row[0]: row[1] for row in cur.fetchall()}
        hyaluronic_id = ing_map["test_hyaluronic_acid"]
        ethanol_id = ing_map["test_ethanol"]

        # 제품 임베딩 및 생성 (동일한 베이스 벡터로 세팅하여 soft-ranking 가중치만을 격리 검증)
        emb_same = embed_text("건조함을 완화해주는 촉촉한 피부 화장품")

        cur.execute(
            """
            INSERT INTO products (name, brand, description, embedding, embedding_model_id)
            VALUES
                ('테스트 에멀전', '브랜드A', '촉촉한 에멀전 제형', %s, 'bge-m3'),
                ('테스트 오일', '브랜드B', '오일 타입 에센스', %s, 'bge-m3'),
                ('테스트 수분크림', '브랜드A', '수분 젤 수분크림', %s, 'bge-m3')
            RETURNING name, product_id;
            """,
            (emb_same, emb_same, emb_same),
        )
        prod_map = {row[0]: row[1] for row in cur.fetchall()}
        emulsion_id = prod_map["테스트 에멀전"]
        oil_id = prod_map["테스트 오일"]
        cream_id = prod_map["테스트 수분크림"]

        # 제품 성분 junction 매핑 — 오일도 그래프 추천성분(히알루론산)을 포함시켜 두어
        # 그래프 성분 필터를 통과하게 하고, 제형 soft-rank(에멀전>오일) 만 격리 검증한다.
        # 에탄올은 오일에만 추가로 넣어 하드필터 검증(마지막 단락)에 쓴다.
        cur.execute(f"""
            INSERT INTO product_ingredients (product_id, ingredient_id) VALUES
            ({emulsion_id}, {hyaluronic_id}),
            ({oil_id}, {hyaluronic_id}),
            ({oil_id}, {ethanol_id}),
            ({cream_id}, {hyaluronic_id});
            """)

        # 문서(RAG용) 삽입 — 그래프 W1 경로의 근거(source_doc_ids)로도 재사용한다.
        emb_doc = embed_text(
            "건조하고 민감한 피부에는 히알루론산 성분이 든 에멀전 로션이 탁월합니다."
        )
        cur.execute(
            """
            INSERT INTO documents (content, embedding, embedding_model_id, source_meta)
            VALUES (
                '건조하고 민감한 피부에는 히알루론산 성분이 든 에멀전 로션이 탁월합니다.',
                %s, 'bge-m3', '{"url": "test_source"}'::jsonb
            )
            RETURNING doc_id;
            """,
            (emb_doc,),
        )
        doc_id = cur.fetchone()[0]

    # 3. 그래프 W1 메타패스(히알루론산→보습→dryness) 적재, 근거문서=위 RAG 문서
    _seed_w1_dryness(db_conn, hyaluronic_id, doc_id)

    # 4. 사용자 memories 데이터 삽입(RLS scope 하에서)
    with db.user_scope(db_conn, 1):
        # 가을 건조 고민 등록
        fact_concern = ExtractedFact(
            fact_type=FactType.HAS_CONCERN,
            content="가을철 건조",
            target_name="건조",
            season="가을",
        )
        decision_concern = CrudDecision(op=CrudOp.ADD, fact=fact_concern)
        crud.apply_decision(db_conn, 1, decision_concern)

        # 제형선호 (other 팩트는 RLS memories만 적재하고 그래프는 없음)
        fact_other = ExtractedFact(
            fact_type=FactType.OTHER,
            content="끈적한 제형 싫어 오일 아님, 에멀전인데 보습 확실",
        )
        decision_other = CrudDecision(op=CrudOp.ADD, fact=fact_other)
        crud.apply_decision(db_conn, 1, decision_other)

        # 5. 검색 융합 API 실행 검증 (season='가을', 질의에 '건조' 키워드 포함 → dryness 인식)
        context = retrieve_recommendation_context(
            db_conn,
            user_id=1,
            query="건조 피부 보습 화장품",
            season="가을",
            limit=50,
        )

    assert context.query == "건조 피부 보습 화장품"

    # 가. RAG/그래프 근거 문서 매칭 검증(그래프 근거가 우선 병합됨)
    assert len(context.doc_hits) >= 1
    assert "히알루론산" in context.doc_hits[0].content

    # 나. 그래프 경로(W1) 수집 검증 — 그래프는 이전 테스트 실행분의 잔여 Ingredient 노드가
    # 누적될 수 있으므로(비파괴적 청소 정책), 이번 실행에서 생성한 hyaluronic_id 로 특정한다.
    assert len(context.graph_paths) >= 1
    w1_path = next(
        p for p in context.graph_paths if len(p.nodes) == 3 and p.nodes[2].key == str(hyaluronic_id)
    )
    assert w1_path.nodes[2].key == str(hyaluronic_id)

    # 다. 개인 memories 회상 검증
    assert len(context.memory_facts) >= 2

    # 라. 제형 soft-ranking 검증 (에멀전이 오일보다 상위 랭크되어야 함, 둘 다 그래프
    # 추천성분인 히알루론산을 포함하므로 그래프 필터를 통과한다)
    product_names = [p.name for p in context.products]
    assert "테스트 에멀전" in product_names
    assert "테스트 오일" in product_names

    idx_emulsion = product_names.index("테스트 에멀전")
    idx_oil = product_names.index("테스트 오일")
    assert idx_emulsion < idx_oil

    # 마. Hard-filter 검증
    with db.user_scope(db_conn, 1):
        # 유저 1번 기억에 기피 성분(테스트에탄올)을 실시간 추가(1B.5 저장은 관계형만으로 충분 —
        # 하드필터는 memories.target_ingredient_id 를 직접 쓰고 그래프에 의존하지 않는다)
        fact_avoid = ExtractedFact(
            fact_type=FactType.AVOID_INGREDIENT,
            content="에탄올 기피",
            target_name="테스트에탄올",
        )
        decision_avoid = CrudDecision(op=CrudOp.ADD, fact=fact_avoid)
        crud.apply_decision(db_conn, 1, decision_avoid, target_ingredient_id=ethanol_id)

        # 다시 융합 검색 조회
        context_filtered = retrieve_recommendation_context(
            db_conn,
            user_id=1,
            query="건조 피부 보습 화장품",
            season="가을",
            limit=50,
        )
    product_names_filtered = [p.name for p in context_filtered.products]

    # 에탄올이 든 테스트 오일 제품은 완전 0건으로 하드 필터링되어 나타나지 않아야 함
    assert "테스트 오일" not in product_names_filtered
    assert "테스트 에멀전" in product_names_filtered
