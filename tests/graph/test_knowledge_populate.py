"""그래프 전역 지식 적재 단위 테스트 (WBS 1A.4)."""

from __future__ import annotations

import os

import psycopg
import pytest

from skinmate.graph import choke
from skinmate.graph.knowledge_populate import populate_global_knowledge


@pytest.fixture(name="db_conn")
def fixture_db_conn():
    """테스트용 DB 연결 피스처. superuser 권한(skinmate) 접속 및 테스트 후 자동 롤백."""
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://skinmate:skinmate-dev-only@localhost:5432/skinmate",
    )
    try:
        with psycopg.connect(db_url, autocommit=False) as conn:
            yield conn
            conn.rollback()
    except psycopg.OperationalError:
        pytest.skip("database connection failed, skipping graph populate unit test.")


def test_populate_global_knowledge_integration(db_conn: psycopg.Connection) -> None:
    """RDB 데이터와 성분 소개문 분석을 통한 전역 지식 엣지 생성 로직을 검증합니다."""
    with db_conn.cursor() as cur:
        # 1. 기존 데이터 초기화 (Apache AGE 그래프 삭제 후 온톨로지 DDL 재설정)
        cur.execute("SET LOCAL search_path = ag_catalog, public;")
        cur.execute("SELECT count(*) FROM ag_graph WHERE name = 'skinmate';")
        res = cur.fetchone()
        assert res is not None
        if res[0] > 0:
            cur.execute("SELECT drop_graph('skinmate', true);")
        cur.execute("SELECT create_graph('skinmate');")

        # DDL 라벨들 적재
        vlabels = ["User", "Ingredient", "Product", "Concern", "Brand"]
        elabels = [
            "CONTAINS",
            "TREATS",
            "AGGRAVATES",
            "HELPS",
            "CONFLICTS",
            "HAS_CONCERN",
            "AVOIDS",
            "PREFERS",
        ]
        for lbl in vlabels:
            cur.execute(f"SELECT create_vlabel('skinmate', '{lbl}');")
        for lbl in elabels:
            cur.execute(f"SELECT create_elabel('skinmate', '{lbl}');")

        # 2. 테스트용 RDB 데이터 삽입
        cur.execute("SET LOCAL search_path = public;")
        cur.execute(
            """
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
                '피부에 일시적인 청량감을 주나, 지속 사용 시 자극 유발 및 붉어짐 '
                '문제가 발생할 수 있어 민감한 피부는 주의할 것.'
            ),
            ('retinol', '레티놀', '주름을 개선함.'),
            ('alcohol', '에탄올', '에탄올 용제.')
            ON CONFLICT (canonical_key) DO UPDATE 
            SET name_ko = EXCLUDED.name_ko, intro = EXCLUDED.intro
            RETURNING canonical_key, ingredient_id;
            """
        )
        ing_map = {row[0]: row[1] for row in cur.fetchall()}
        hyaluronic_id = ing_map["test_hyaluronic_acid"]
        ing_map["test_ethanol"]

        cur.execute(
            """
            INSERT INTO products (name, brand, description)
            VALUES ('테스트 에멀전', '테스트브랜드', '수분 에멀전')
            RETURNING product_id;
            """
        )
        emulsion_id = cur.fetchone()[0]

        cur.execute(
            f"""
            INSERT INTO product_ingredients (product_id, ingredient_id) VALUES
            ({emulsion_id}, {hyaluronic_id});
            """
        )

    # 3. 전역 지식 적재 스크립트 실행
    populate_global_knowledge(db_conn)

    # 4. Apache AGE 그래프 노드 및 엣지 MERGE 상태 조회 검증 (user_scope=None으로 전역 조회)
    # DatatypeMismatch 방지를 위해 복수 컬럼 대신 Map형태 {key: value}로 리턴합니다.
    
    # 가. Concern 노드 개수 검증
    concerns = choke.age_exec(
        db_conn,
        None,
        "MATCH (c:Concern) RETURN {name: c.name, label: c.label}",
    )
    concern_names = {c["name"] for c in concerns}
    assert "dryness" in concern_names
    assert "sensitivity" in concern_names
    assert "pores" in concern_names

    # 나. Ingredient 노드 검증
    ingredients_graph = choke.age_exec(
        db_conn,
        None,
        "MATCH (i:Ingredient {canonical_key: 'test_hyaluronic_acid'}) RETURN {name: i.name}",
    )
    assert len(ingredients_graph) == 1
    assert ingredients_graph[0]["name"] == "테스트히알루론산"

    # 다. Product 노드 검증
    products_graph = choke.age_exec(
        db_conn,
        None,
        f"MATCH (p:Product {{product_id: {emulsion_id}}}) RETURN {{name: p.name, brand: p.brand}}",
    )
    assert len(products_graph) == 1
    assert products_graph[0]["name"] == "테스트 에멀전"
    assert products_graph[0]["brand"] == "테스트브랜드"

    # 라. CONTAINS 엣지 검증
    contains_edges = choke.age_exec(
        db_conn,
        None,
        f"MATCH (p:Product {{product_id: {emulsion_id}}})-[r:CONTAINS]->(i:Ingredient) "
        "RETURN {key: i.canonical_key}",
    )
    assert len(contains_edges) == 1
    assert contains_edges[0]["key"] == "test_hyaluronic_acid"

    # 마. TREATS 엣지 검증 (보습 키워드로 인해 dryness와 매칭)
    treats_edges = choke.age_exec(
        db_conn,
        None,
        "MATCH (i:Ingredient {canonical_key: 'test_hyaluronic_acid'})-[r:TREATS]->(c:Concern) "
        "RETURN {name: c.name}",
    )
    assert len(treats_edges) == 1
    assert treats_edges[0]["name"] == "dryness"

    # 바. AGGRAVATES 엣지 검증 (자극 유발 키워드로 인해 sensitivity와 매칭)
    aggravates_edges = choke.age_exec(
        db_conn,
        None,
        "MATCH (i:Ingredient {canonical_key: 'test_ethanol'})-[r:AGGRAVATES]->(c:Concern) "
        "RETURN {name: c.name}",
    )
    assert len(aggravates_edges) == 1
    assert aggravates_edges[0]["name"] == "sensitivity"

    # 사. HELPS / CONFLICTS 엣지 검증 (retinol CONFLICTS alcohol 관계 검증)
    conflict_edges = choke.age_exec(
        db_conn,
        None,
        "MATCH (i1:Ingredient {canonical_key: 'retinol'})-[r:CONFLICTS]->(i2:Ingredient) "
        "RETURN {key: i2.canonical_key}",
    )
    assert len(conflict_edges) == 1
    assert conflict_edges[0]["key"] == "alcohol"
