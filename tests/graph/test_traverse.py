"""GraphRAG 개념 기반 메타패스(W1/W2) 순회 및 근거 문장 생성 단위 테스트.

scripts/graphrag_slice.py 로 검증된 W1(성분→작동원리→고민)·W2(성분→고민 직접) 순회를
프로덕션(graph/traverse.py)으로 승격한 코드를 검증한다. 그래프는 ingredient_id 키의
Ingredient 노드·Mechanism 노드·ACHIEVES/TREATS(+source_doc_ids) 표현을 쓴다(옛
canonical_key 기반 AVOIDS/PREFERS/대안경로 표현은 더 이상 이 함수가 방출하지 않는다).
"""

from __future__ import annotations

import psycopg

from skinmate import db
from skinmate.contracts.facts import FactType
from skinmate.contracts.graph import EdgeRel, NodeKind
from skinmate.graph import choke
from skinmate.graph.traverse import (
    generate_rationale_from_path,
    recognize_concern,
    traverse_recommendation_paths,
)
from skinmate.memory import crud
from skinmate.memory.crud import CrudDecision, CrudOp
from skinmate.memory.extract import ExtractedFact


def _ensure_graph_labels(cur: psycopg.Cursor) -> None:
    """User/Ingredient/Product/Concern/Brand/Mechanism vlabel + ACHIEVES/ENABLES 포함
    관계 elabel을 멱등 보장한다."""
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


def _seed_ingredients(cur: psycopg.Cursor) -> dict[str, int]:
    cur.execute("""
        INSERT INTO ingredients (canonical_key, name_ko, intro)
        VALUES
        ('test_gr_hyaluronic', '테스트히알루론산', 'W1 검증용 보습 성분.'),
        ('test_gr_niacinamide', '테스트나이아신아마이드', 'W2 검증용 직접 완화 성분.')
        ON CONFLICT (canonical_key) DO UPDATE
        SET name_ko = EXCLUDED.name_ko, intro = EXCLUDED.intro
        RETURNING canonical_key, ingredient_id;
        """)
    return {row[0]: row[1] for row in cur.fetchall()}


def _seed_w1_w2_dryness(conn: psycopg.Connection, hyaluronic_id: int, niacinamide_id: int) -> None:
    """W1(히알루론산 -[ACHIEVES]-> 보습 -[TREATS]-> dryness) +
    W2(나이아신아마이드 -[TREATS]-> dryness) 트리플을 멱등 적재한다(graphrag_slice.py 패턴:
    노드는 각각 MERGE로 먼저 바인딩, 관계 속성은 별도 MATCH+SET)."""
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
        {"id": hyaluronic_id, "mech": "보습", "docs": [9101]},
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
        {"mech": "보습", "concern": "dryness", "docs": [9102]},
    )

    choke.age_exec(
        conn,
        None,
        "MERGE (i:Ingredient {ingredient_id: $id}) SET i.name_ko = $name",
        {"id": niacinamide_id, "name": "테스트나이아신아마이드"},
    )
    choke.age_exec(
        conn,
        None,
        "MERGE (i:Ingredient {ingredient_id: $id}) MERGE (c:Concern {name: $concern}) "
        "MERGE (i)-[:TREATS]->(c)",
        {"id": niacinamide_id, "concern": "dryness"},
    )
    choke.age_exec(
        conn,
        None,
        "MATCH (i:Ingredient {ingredient_id: $id})-[r:TREATS]->(c:Concern {name: $concern}) "
        "SET r.source_doc_ids = $docs, r.origin = 'manual', r.confidence = 1.0",
        {"id": niacinamide_id, "concern": "dryness", "docs": [9103]},
    )


def test_recognize_concern_from_query_keywords() -> None:
    """질의 키워드로 고민 코드를 인식한다(순수 함수, DB 불필요)."""
    assert recognize_concern("여드름에 좋은 제품 추천해줘") == "acne"
    assert recognize_concern("건조해서 보습 제품 찾아요") == "dryness"
    assert recognize_concern("모공이 넓어져서 고민이에요") == "pores"
    assert recognize_concern("오늘 날씨가 참 좋네요") is None


def test_traverse_w1_w2_metapaths_and_rationale(db_conn: psycopg.Connection) -> None:
    """질의에서 인식한 고민을 기점으로 W1·W2 경로와 근거 문서(source_doc_ids)가
    함께 조립되고, generate_rationale_from_path 가 자연어 이유를 만드는지 검증한다."""
    with db_conn.cursor() as cur:
        _ensure_graph_labels(cur)
        cur.execute("SET LOCAL search_path = public;")
        ing_map = _seed_ingredients(cur)
    hyaluronic_id = ing_map["test_gr_hyaluronic"]
    niacinamide_id = ing_map["test_gr_niacinamide"]

    _seed_w1_w2_dryness(db_conn, hyaluronic_id, niacinamide_id)
    db_conn.commit()

    paths = traverse_recommendation_paths(
        db_conn, user_id=1, query="건조한 피부에 좋은 성분 추천해줘"
    )

    # W1: [Concern, Mechanism, Ingredient]
    w1_paths = [p for p in paths if len(p.nodes) == 3]
    matching_w1 = [p for p in w1_paths if p.nodes[2].key == str(hyaluronic_id)]
    assert matching_w1, "W1(히알루론산→보습→건조) 경로가 없음"
    p1 = matching_w1[0]
    assert p1.nodes[0].kind == NodeKind.CONCERN
    assert p1.nodes[0].key == "dryness"
    assert p1.nodes[0].label == "건조"
    assert p1.nodes[1].kind == NodeKind.MECHANISM
    assert p1.nodes[1].key == "보습"
    assert p1.nodes[2].kind == NodeKind.INGREDIENT
    assert p1.nodes[2].label == "테스트히알루론산"

    achieves_edge = next(e for e in p1.edges if e.rel == EdgeRel.ACHIEVES)
    assert achieves_edge.from_idx == 2
    assert achieves_edge.to_idx == 1
    assert achieves_edge.source_doc_ids == [9101]
    treats_edge = next(e for e in p1.edges if e.rel == EdgeRel.TREATS)
    assert treats_edge.from_idx == 1
    assert treats_edge.to_idx == 0
    assert treats_edge.source_doc_ids == [9102]

    rationale_w1 = generate_rationale_from_path(p1)
    assert "테스트히알루론산" in rationale_w1
    assert "보습" in rationale_w1
    assert "건조" in rationale_w1

    # W2: [Concern, Ingredient]
    w2_paths = [p for p in paths if len(p.nodes) == 2]
    matching_w2 = [p for p in w2_paths if p.nodes[1].key == str(niacinamide_id)]
    assert matching_w2, "W2(나이아신아마이드→건조) 경로가 없음"
    p2 = matching_w2[0]
    assert p2.nodes[0].kind == NodeKind.CONCERN
    assert p2.nodes[1].kind == NodeKind.INGREDIENT
    assert p2.nodes[1].label == "테스트나이아신아마이드"
    treats_edge2 = next(e for e in p2.edges if e.rel == EdgeRel.TREATS)
    assert treats_edge2.from_idx == 1
    assert treats_edge2.to_idx == 0
    assert treats_edge2.source_doc_ids == [9103]

    rationale_w2 = generate_rationale_from_path(p2)
    assert "테스트나이아신아마이드" in rationale_w2
    assert "건조" in rationale_w2


def test_traverse_no_concern_recognized_returns_empty(db_conn: psycopg.Connection) -> None:
    """질의에 고민 키워드가 없고 사용자 HAS_CONCERN 기억도 없으면 빈 리스트를 반환한다."""
    paths = traverse_recommendation_paths(db_conn, user_id=555001, query="오늘 기분이 좋아요")
    assert paths == []


def test_traverse_concern_fallback_from_has_concern_memory(db_conn: psycopg.Connection) -> None:
    """질의에 고민 키워드가 없어도 사용자의 HAS_CONCERN 기억(한글 라벨 저장 관례)으로
    폴백 인식해 W1/W2 순회를 수행한다."""
    with db_conn.cursor() as cur:
        _ensure_graph_labels(cur)
        cur.execute("SET LOCAL search_path = public;")
        ing_map = _seed_ingredients(cur)
    hyaluronic_id = ing_map["test_gr_hyaluronic"]
    niacinamide_id = ing_map["test_gr_niacinamide"]
    _seed_w1_w2_dryness(db_conn, hyaluronic_id, niacinamide_id)
    db_conn.commit()

    uid = 555002
    with db.user_scope(db_conn, uid):
        fact = ExtractedFact(
            fact_type=FactType.HAS_CONCERN, content="건조한 편이에요", target_name="건조"
        )
        decision = CrudDecision(op=CrudOp.ADD, fact=fact)
        crud.apply_decision(db_conn, uid, decision)

    paths = traverse_recommendation_paths(db_conn, user_id=uid, query="이거 괜찮을까요?")
    assert any(
        p.nodes[0].key == "dryness" for p in paths
    ), "HAS_CONCERN 기억(라벨 저장)으로부터 고민 폴백 인식이 되지 않음"
