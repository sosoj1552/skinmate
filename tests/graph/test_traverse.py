"""WBS 1A.6 2+hop 그래프 순회 및 근거 문장 생성 단위 테스트."""

from __future__ import annotations

import os

import psycopg
import pytest

from skinmate.contracts.graph import EdgeRel, NodeKind
from skinmate.graph.knowledge_populate import populate_global_knowledge
from skinmate.graph.traverse import (
    generate_rationale_from_path,
    traverse_recommendation_paths,
)


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
        pytest.skip("database connection failed, skipping graph traverse unit test.")


def test_traverse_recommendation_paths_integration(db_conn: psycopg.Connection) -> None:
    """사용자 memories와 RDB 지식을 바탕으로 2+hop 그래프 순회 및 격리(leakage)를 검증합니다."""
    with db_conn.cursor() as cur:
        # 1. 기존 데이터 초기화 (Apache AGE 그래프 DDL 세팅)
        cur.execute("SET LOCAL search_path = ag_catalog, public;")
        cur.execute("SELECT count(*) FROM ag_graph WHERE name = 'skinmate';")
        res = cur.fetchone()
        assert res is not None
        if res[0] > 0:
            cur.execute("SELECT drop_graph('skinmate', true);")
        cur.execute("SELECT create_graph('skinmate');")

        # DDL 라벨 생성
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
                '피부에 강력한 보습 효과를 주어 건조함을 해결하지만, '
                '지속 사용 시 자극 유발 및 붉어짐 '
                '문제가 발생할 수 있어 민감한 피부는 주의할 것.'
            ),
            ('retinol', '레티놀', '주름을 개선함.'),
            ('alcohol', '에탄올', '에탄올 용제.')
            ON CONFLICT (canonical_key) DO UPDATE 
            SET name_ko = EXCLUDED.name_ko, intro = EXCLUDED.intro
            RETURNING canonical_key, ingredient_id;
            """)
        ing_map = {row[0]: row[1] for row in cur.fetchall()}
        hyaluronic_id = ing_map["test_hyaluronic_acid"]
        ethanol_id = ing_map["test_ethanol"]

        cur.execute("""
            INSERT INTO products (name, brand, description)
            VALUES 
                ('테스트 에멀전', '테스트브랜드', '수분 에멀전'),
                ('테스트 오일', '테스트브랜드', '자극 에탄올 오일')
            RETURNING name, product_id;
            """)
        prod_map = {row[0]: row[1] for row in cur.fetchall()}
        emulsion_id = prod_map["테스트 에멀전"]
        oil_id = prod_map["테스트 오일"]

        # 제품 성분 junction 테이블 매핑
        cur.execute(f"""
            INSERT INTO product_ingredients (product_id, ingredient_id) VALUES
            ({emulsion_id}, {hyaluronic_id}),
            ({oil_id}, {ethanol_id});
            """)

        # 3. 사용자 memories (회피/선호/고민) 데이터 삽입
        # 유저 1번:
        #  - 기피 성분: 테스트에탄올 (test_ethanol)
        #  - 선호 성분: 테스트히알루론산 (test_hyaluronic_acid)
        #  - 계절 고민: 가을철 dryness
        # 유저 2번:
        #  - 기피 성분: 테스트히알루론산 (test_hyaluronic_acid) -> 1번의 선호성분과 격리 대치됨
        cur.execute(f"""
            INSERT INTO memories (
                user_id, content, fact_type, target_ingredient_id, 
                target_name, season, base_weight, frequency, last_seen
            )
            VALUES
                (1, '에탄올 피함', 'avoid_ingredient', {ethanol_id}, 
                 NULL, NULL, 1.0, 1, NOW()),
                (1, '히알루론산 선호', 'prefer_ingredient', {hyaluronic_id}, 
                 NULL, NULL, 1.0, 1, NOW()),
                (1, '가을철 건조', 'has_concern', NULL, 
                 'dryness', '가을', 1.0, 1, NOW()),
                (2, '히알루론산 기피', 'avoid_ingredient', {hyaluronic_id}, 
                 NULL, NULL, 1.0, 1, NOW());
            """)

    # 4. 전역 지식 및 사용자 memories 그래프 투영 실행
    populate_global_knowledge(db_conn)

    # 5. 유저 1번 그래프 2+hop 순회 및 근거 문장 생성 검증
    paths_user1 = traverse_recommendation_paths(db_conn, user_id=1, season="가을")

    # 순회 경로 수집 확인
    avoid_paths = [p for p in paths_user1 if EdgeRel.AVOIDS in {e.rel for e in p.edges}]
    prefer_paths = [p for p in paths_user1 if EdgeRel.PREFERS in {e.rel for e in p.edges}]
    treat_paths = [p for p in paths_user1 if EdgeRel.HAS_CONCERN in {e.rel for e in p.edges}]
    alt_paths = [p for p in paths_user1 if len(p.nodes) == 5]

    # 가. Avoidance Path 검증 (기피하는 에탄올이 든 오일 제품)
    assert len(avoid_paths) >= 1
    p_avoid = avoid_paths[0]
    assert p_avoid.nodes[0].kind == NodeKind.USER
    assert p_avoid.nodes[1].key == "test_ethanol"
    assert p_avoid.nodes[2].key == str(oil_id)
    rationale_avoid = generate_rationale_from_path(p_avoid)
    assert "기피 성분" in rationale_avoid
    assert "테스트에탄올" in rationale_avoid

    # 나. Preference Path 검증 (선호하는 히알루론산이 든 에멀전 제품)
    assert len(prefer_paths) >= 1
    p_prefer = prefer_paths[0]
    assert p_prefer.nodes[1].key == "test_hyaluronic_acid"
    assert p_prefer.nodes[2].key == str(emulsion_id)
    rationale_prefer = generate_rationale_from_path(p_prefer)
    assert "선호하시는" in rationale_prefer
    assert "테스트히알루론산" in rationale_prefer

    # 다. Treatment Path 검증 (가을철 dryness 고민 완화)
    assert len(treat_paths) >= 1
    p_treat = treat_paths[0]
    assert p_treat.nodes[1].key == "dryness"
    assert p_treat.nodes[2].key == "test_hyaluronic_acid"
    assert p_treat.nodes[3].key == str(emulsion_id)
    # 엣지 프로퍼티 season 검증
    concern_edge = next(e for e in p_treat.edges if e.rel == EdgeRel.HAS_CONCERN)
    assert concern_edge.season == "가을"
    rationale_treat = generate_rationale_from_path(p_treat)
    assert "가을철 고민인" in rationale_treat
    assert "건조" in rationale_treat or "dryness" in rationale_treat

    # 라. Alternative Path 검증 (기피하는 에탄올 대신 건조를 해결하는 대안인 히알루론산 에멀전 추천)
    assert len(alt_paths) >= 1
    p_alt = alt_paths[0]
    assert p_alt.nodes[1].key == "test_ethanol"
    assert p_alt.nodes[2].key == "dryness"
    assert p_alt.nodes[3].key == "test_hyaluronic_acid"
    assert p_alt.nodes[4].key == str(emulsion_id)
    rationale_alt = generate_rationale_from_path(p_alt)
    assert "대체 성분" in rationale_alt
    assert "테스트에탄올" in rationale_alt
    assert "테스트히알루론산" in rationale_alt

    # 6. 유저 격리 및 0-row leakage 검증 (AC-G3)
    # 유저 2번 순회 결과 검증
    paths_user2 = traverse_recommendation_paths(db_conn, user_id=2)

    # 유저 2번의 기피성분 경로
    avoid_paths_user2 = [p for p in paths_user2 if EdgeRel.AVOIDS in {e.rel for e in p.edges}]
    assert len(avoid_paths_user2) >= 1
    # 유저 2번의 기피 성분은 히알루론산이어야 함
    assert avoid_paths_user2[0].nodes[1].key == "test_hyaluronic_acid"
    assert avoid_paths_user2[0].nodes[2].key == str(emulsion_id)

    # 0-row leakage: 유저 2번의 순회결과에 유저 1번 개인의
    # 선호성분/고민 엣지가 절대 섞이지 않았는지 검증
    prefer_paths_user2 = [p for p in paths_user2 if EdgeRel.PREFERS in {e.rel for e in p.edges}]
    treat_paths_user2 = [p for p in paths_user2 if EdgeRel.HAS_CONCERN in {e.rel for e in p.edges}]

    # 유저 2번은 선호성분과 고민이 등록되어 있지 않으므로 0건이어야 함
    assert len(prefer_paths_user2) == 0
    assert len(treat_paths_user2) == 0
