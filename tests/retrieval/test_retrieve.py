"""WBS 1A.7 검색 3종 합치기 및 제형 soft-ranking 단위 테스트."""

from __future__ import annotations

import psycopg

from skinmate.contracts.facts import FactType
from skinmate.documents.embed import embed_text
from skinmate.graph.knowledge_populate import populate_global_knowledge
from skinmate.memory import bridge, crud
from skinmate.memory.crud import CrudDecision, CrudOp
from skinmate.memory.extract import ExtractedFact
from skinmate.retrieval.retrieve import retrieve_recommendation_context


def test_retrieve_recommendation_context_integration(db_conn: psycopg.Connection) -> None:
    """RAG, 그래프, 기억이 융합된 RetrievalContext 및 제형 soft-ranking 작동을 검증합니다."""
    with db_conn.cursor() as cur:
        # 1. 멱등적 그래프 및 라벨 생성 (비파괴적 셋업)
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
                    SELECT 1 FROM ag_catalog.ag_label 
                    WHERE name = lbl AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_vlabel(g, lbl);
                END IF;
            END LOOP;
            FOREACH lbl IN ARRAY elabels LOOP
                IF NOT EXISTS (
                    SELECT 1 FROM ag_catalog.ag_label 
                    WHERE name = lbl AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_elabel(g, lbl);
                END IF;
            END LOOP;
        END $$;
        """)

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

        # 제품 성분 junction 매핑
        cur.execute(f"""
            INSERT INTO product_ingredients (product_id, ingredient_id) VALUES
            ({emulsion_id}, {hyaluronic_id}),
            ({oil_id}, {ethanol_id}),
            ({cream_id}, {hyaluronic_id});
            """)

        # 문서(RAG용) 삽입
        emb_doc = embed_text(
            "건조하고 민감한 피부에는 히알루론산 성분이 든 에멀전 로션이 탁월합니다."
        )
        cur.execute(
            """
            INSERT INTO documents (content, embedding, embedding_model_id, source_meta)
            VALUES (
                '건조하고 민감한 피부에는 히알루론산 성분이 든 에멀전 로션이 탁월합니다.', 
                %s, 'bge-m3', '{"url": "test_source"}'::jsonb
            );
            """,
            (emb_doc,),
        )

    # 4. 그래프 전역 지식만 투영 (memories 개인 엣지는 1B.5 bridge 호출로 실시간 삽입)
    populate_global_knowledge(db_conn)

    # 3. 사용자 memories 데이터 삽입 (1B.5 bridge.project_to_graph 실시간 연동)
    # memories 조작은 RLS scope(user_id=1) 하에서 실행해야 함
    from skinmate import db

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
        bridge.project_to_graph(db_conn, 1, decision_concern, concern_key="dryness")

        # 제형선호 (other 팩트는 RLS memories만 적재하고 그래프는 없음)
        fact_other = ExtractedFact(
            fact_type=FactType.OTHER,
            content="끈적한 제형 싫어 오일 아님, 에멀전인데 보습 확실",
        )
        decision_other = CrudDecision(op=CrudOp.ADD, fact=fact_other)
        crud.apply_decision(db_conn, 1, decision_other)

        # 5. 검색 융합 API 실행 검증 (season='가을')
        context = retrieve_recommendation_context(
            db_conn,
            user_id=1,
            query="건조 피부 보습 화장품",
            season="가을",
            limit=50,
        )

    assert context.query == "건조 피부 보습 화장품"

    # 가. RAG 문서 매칭 검증
    assert len(context.doc_hits) >= 1
    assert "히알루론산" in context.doc_hits[0].content

    # 나. 그래프 경로 수집 검증
    assert len(context.graph_paths) >= 1

    # 다. 개인 memories 회상 검증
    assert len(context.memory_facts) >= 2

    # 라. 제형 soft-ranking 검증 (에멀전이 오일보다 상위 랭크되어야 함)
    product_names = [p.name for p in context.products]
    assert "테스트 에멀전" in product_names
    assert "테스트 오일" in product_names

    idx_emulsion = product_names.index("테스트 에멀전")
    idx_oil = product_names.index("테스트 오일")
    assert idx_emulsion < idx_oil

    # 마. Hard-filter 검증
    with db.user_scope(db_conn, 1):
        # 유저 1번 기억에 기피 성분(테스트에탄올)을 실시간 추가 및 투영 (1B.5 실시간 동기화)
        fact_avoid = ExtractedFact(
            fact_type=FactType.AVOID_INGREDIENT,
            content="에탄올 기피",
            target_name="테스트에탄올",
        )
        decision_avoid = CrudDecision(op=CrudOp.ADD, fact=fact_avoid)
        crud.apply_decision(db_conn, 1, decision_avoid, target_ingredient_id=ethanol_id)
        bridge.project_to_graph(db_conn, 1, decision_avoid, ingredient_key="test_ethanol")

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
