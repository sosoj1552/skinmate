"""RDB 데이터를 Apache AGE 그래프 온톨로지에 투영하는 전역 지식 적재 엔진 (WBS 1A.4).

ingredients, products, product_ingredients 정보를 노드 및 CONTAINS 엣지로 MERGE하고,
성분 소개문(intro) 텍스트를 파싱하여 TREATS/AGGRAVATES 엣지를,
사전 정의된 성분 궁합을 HELPS/CONFLICTS 엣지로 자동 적재합니다.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg
import structlog

logger = structlog.get_logger()

# 고민 정합성 및 키워드 규칙 정의 (유동적 확장 가능)
CONCERN_RULES: dict[str, dict[str, Any]] = {
    "dryness": {
        "label": "건조",
        "keywords": ["보습", "수분", "건조", "속건조", "습윤"],
    },
    "sensitivity": {
        "label": "민감",
        "keywords": ["진정", "자극 완화", "장벽 강화", "민감", "붉은기 완화"],
    },
    "acne": {
        "label": "트러블",
        "keywords": ["여드름", "트러블", "염증", "피지 조절", "각질 케어", "뾰루지"],
    },
    "wrinkles": {
        "label": "주름",
        "keywords": ["주름", "탄력", "안티에이징", "노화", "탄력 개선"],
    },
    "oiliness": {
        "label": "피지",
        "keywords": ["피지 조절", "유분 조절", "피지 흡착", "매트", "번들거림 개선"],
    },
    "dullness": {
        "label": "칙칙함",
        "keywords": ["칙칙", "피부톤", "미백", "브라이트닝", "환하게", "기미"],
    },
    "pores": {
        "label": "모공",
        "keywords": ["모공", "수렴", "모공 축소", "모공 케어", "모공 타이트닝"],
    },
}

# AGGRAVATES 엣지 추출 규칙 (주로 민감성 피부 대상 자극원 판별)
AGGRAVATES_RULES: dict[str, dict[str, list[str]]] = {
    "sensitivity": {
        "bad_words": ["자극 유발", "민감한 피부는 주의", "붉어짐 유발", "피부 자극", "자극적"],
        "good_words": ["완화", "진정", "도움", "개선"],
    }
}

# 성분-성분 궁합 기본 지식 규칙 (canonical_key 기준)
PARTNERSHIPS = [
    ("retinol", "CONFLICTS", "alcohol"),
    ("retinol", "HELPS", "hyaluronic_acid"),
    ("ascorbic_acid", "CONFLICTS", "retinol"),
    ("salicylic_acid", "CONFLICTS", "retinol"),
    ("glycolic_acid", "CONFLICTS", "retinol"),
    ("niacinamide", "HELPS", "retinol"),
    ("ceramide", "HELPS", "retinol"),
]


def safe_str(val: Any) -> str:
    """Cypher 더블 쿼테이션 내에서 안전하도록 문자열을 이스케이프 처리합니다."""
    if val is None:
        return ""
    # 백슬래시와 더블 쿼테이션을 이스케이프하여 문법 충돌 방지
    return str(val).replace("\\", "\\\\").replace('"', '\\"')


def populate_global_knowledge(conn: psycopg.Connection[Any]) -> None:
    """RDB 데이터와 성분 텍스트를 기반으로 Apache AGE 그래프 온톨로지를 빌드합니다.

    본 함수는 멱등성(idempotent)을 보장합니다.
    Apache AGE의 불안정한 파라미터 바인딩 결함 및 다중 구문 파서 폭발(Crash)을 방지하기 위해,
    안전한 단일 cypher 호출 SQL 구문들을 세미콜론(;)으로 묶어 배치 실행하는 아키텍처를 사용합니다.
    """
    logger.info("starting_graph_global_knowledge_populate")

    with conn.cursor() as cur:
        # age extension 로드 및 search_path 설정
        cur.execute("SET LOCAL search_path = ag_catalog, public;")

        # 1. Concern 노드 MERGE
        logger.info("seeding_concern_nodes")
        concern_sqls = []
        for name, rule in CONCERN_RULES.items():
            concern_sqls.append(
                f"SELECT * FROM cypher('skinmate', $$"
                f'MERGE (c:Concern {{name: "{name}"}}) SET c.label = "{safe_str(rule["label"])}"'
                f"$$) AS (result agtype);"
            )
        cur.execute("\n".join(concern_sqls))

        # 2. Ingredient 노드 MERGE (50개 청크 단위 세미콜론 실행)
        cur.execute("SELECT canonical_key, name_ko FROM ingredients;")
        ingredients = cur.fetchall()
        logger.info("fetched_ingredients_for_graph", count=len(ingredients))

        ing_sqls = []
        for key, name_ko in ingredients:
            safe_key = re.sub(r"[^a-z0-9_]+", "", key.lower())
            if not safe_key:
                continue
            ing_sqls.append(
                f"SELECT * FROM cypher('skinmate', $$"
                f'MERGE (ing:Ingredient {{canonical_key: "{safe_key}"}}) '
                f'SET ing.name = "{safe_str(name_ko)}"'
                f"$$) AS (result agtype);"
            )

        chunk_size = 50
        for i in range(0, len(ing_sqls), chunk_size):
            chunk = ing_sqls[i : i + chunk_size]
            cur.execute("\n".join(chunk))

        # 3. Product 노드 MERGE (일괄 세미콜론 실행)
        cur.execute("SELECT product_id, name, brand FROM products;")
        products = cur.fetchall()
        logger.info("fetched_products_for_graph", count=len(products))

        prod_sqls = []
        for prod_id, name, brand in products:
            prod_sqls.append(
                f"SELECT * FROM cypher('skinmate', $$"
                f"MERGE (p:Product {{product_id: {int(prod_id)}}}) "
                f'SET p.name = "{safe_str(name)}", p.brand = "{safe_str(brand)}"'
                f"$$) AS (result agtype);"
            )
        if prod_sqls:
            cur.execute("\n".join(prod_sqls))

        # 4. CONTAINS 엣지 MERGE (100개 청크 단위 세미콜론 실행)
        cur.execute("""
            SELECT pi.product_id, i.canonical_key
            FROM product_ingredients pi
            JOIN ingredients i ON pi.ingredient_id = i.ingredient_id;
            """)
        mappings = cur.fetchall()
        logger.info("fetched_contains_mappings_for_graph", count=len(mappings))

        contains_sqls = []
        for prod_id, key in mappings:
            safe_key = re.sub(r"[^a-z0-9_]+", "", key.lower())
            if not safe_key:
                continue
            contains_sqls.append(
                f"SELECT * FROM cypher('skinmate', $$"
                f"MATCH (p:Product {{product_id: {int(prod_id)}}}), "
                f'      (ing:Ingredient {{canonical_key: "{safe_key}"}}) '
                f"MERGE (p)-[:CONTAINS]->(ing)"
                f"$$) AS (result agtype);"
            )

        for i in range(0, len(contains_sqls), 100):
            chunk = contains_sqls[i : i + 100]
            cur.execute("\n".join(chunk))

        # 5. TREATS / AGGRAVATES 엣지 자동 추출 및 MERGE (100개 청크 단위 세미콜론 실행)
        cur.execute("SELECT canonical_key, intro FROM ingredients WHERE intro IS NOT NULL;")
        ing_intros = cur.fetchall()
        logger.info("analyzing_ingredient_intros_for_relations", count=len(ing_intros))

        relation_sqls = []
        for key, intro in ing_intros:
            safe_key = re.sub(r"[^a-z0-9_]+", "", key.lower())
            if not safe_key or not intro:
                continue

            # 가. TREATS 관계 검사
            for concern, rule in CONCERN_RULES.items():
                for kw in rule["keywords"]:
                    if kw in intro:
                        relation_sqls.append(
                            f"SELECT * FROM cypher('skinmate', $$"
                            f'MATCH (ing:Ingredient {{canonical_key: "{safe_key}"}}), '
                            f'      (c:Concern {{name: "{concern}"}}) '
                            f"MERGE (ing)-[:TREATS]->(c)"
                            f"$$) AS (result agtype);"
                        )
                        break

            # 나. AGGRAVATES 관계 검사 (민감성 피부 대상 부정 단어 검색)
            for concern, rule in AGGRAVATES_RULES.items():
                has_bad = any(bw in intro for bw in rule["bad_words"])
                has_good = any(gw in intro for gw in rule["good_words"])
                if has_bad and not has_good:
                    relation_sqls.append(
                        f"SELECT * FROM cypher('skinmate', $$"
                        f'MATCH (ing:Ingredient {{canonical_key: "{safe_key}"}}), '
                        f'      (c:Concern {{name: "{concern}"}}) '
                        f"MERGE (ing)-[:AGGRAVATES]->(c)"
                        f"$$) AS (result agtype);"
                    )

        for i in range(0, len(relation_sqls), 100):
            chunk = relation_sqls[i : i + 100]
            cur.execute("\n".join(chunk))

        # 6. HELPS / CONFLICTS 성분-성분 궁합 엣지 MERGE (일괄 세미콜론 실행)
        cur.execute("SELECT canonical_key FROM ingredients;")
        existing_keys = {row[0] for row in cur.fetchall()}

        partnership_sqls = []
        for key_a, rel_type, key_b in PARTNERSHIPS:
            if key_a in existing_keys and key_b in existing_keys:
                partnership_sqls.append(
                    f"SELECT * FROM cypher('skinmate', $$"
                    f'MATCH (ia:Ingredient {{canonical_key: "{key_a}"}}), '
                    f'      (ib:Ingredient {{canonical_key: "{key_b}"}}) '
                    f"MERGE (ia)-[:{rel_type}]->(ib)"
                    f"$$) AS (result agtype);"
                )

        if partnership_sqls:
            cur.execute("\n".join(partnership_sqls))

        # 7. memories 테이블을 조회하여 개인 User 노드 및
        # AVOIDS / PREFERS / HAS_CONCERN 엣지 복구 적재 (세미콜론 실행)
        logger.info("projecting_personal_memories_to_graph")
        # 가. User 노드 생성
        cur.execute("SELECT DISTINCT user_id FROM memories WHERE deleted_at IS NULL;")
        user_ids = [row[0] for row in cur.fetchall()]
        user_sqls = []
        for uid in user_ids:
            user_sqls.append(
                f"SELECT * FROM cypher('skinmate', $$"
                f"MERGE (u:User {{user_id: {int(uid)}}})"
                f"$$) AS (result agtype);"
            )
        if user_sqls:
            cur.execute("\n".join(user_sqls))

        # 나. AVOIDS 엣지 생성
        cur.execute("""
            SELECT m.user_id, i.canonical_key 
            FROM memories m 
            JOIN ingredients i ON m.target_ingredient_id = i.ingredient_id 
            WHERE m.fact_type = 'avoid_ingredient' AND m.deleted_at IS NULL;
            """)
        avoid_mems = cur.fetchall()
        avoid_sqls = []
        for uid, key in avoid_mems:
            safe_key = re.sub(r"[^a-z0-9_]+", "", key.lower())
            if not safe_key:
                continue
            avoid_sqls.append(
                f"SELECT * FROM cypher('skinmate', $$"
                f"MATCH (u:User {{user_id: {int(uid)}}}), "
                f"      (i:Ingredient {{canonical_key: '{safe_key}'}})"
                f"MERGE (u)-[r:AVOIDS {{user_scope: {int(uid)}}}]->(i)"
                f"$$) AS (result agtype);"
            )
        if avoid_sqls:
            cur.execute("\n".join(avoid_sqls))

        # 다. PREFERS 엣지 생성
        cur.execute("""
            SELECT m.user_id, i.canonical_key 
            FROM memories m 
            JOIN ingredients i ON m.target_ingredient_id = i.ingredient_id 
            WHERE m.fact_type = 'prefer_ingredient' AND m.deleted_at IS NULL;
            """)
        prefer_mems = cur.fetchall()
        prefer_sqls = []
        for uid, key in prefer_mems:
            safe_key = re.sub(r"[^a-z0-9_]+", "", key.lower())
            if not safe_key:
                continue
            prefer_sqls.append(
                f"SELECT * FROM cypher('skinmate', $$"
                f"MATCH (u:User {{user_id: {int(uid)}}}), "
                f"      (i:Ingredient {{canonical_key: '{safe_key}'}})"
                f"MERGE (u)-[r:PREFERS {{user_scope: {int(uid)}}}]->(i)"
                f"$$) AS (result agtype);"
            )
        if prefer_sqls:
            cur.execute("\n".join(prefer_sqls))

        # 라. HAS_CONCERN 엣지 생성
        cur.execute("""
            SELECT user_id, target_name, season 
            FROM memories 
            WHERE fact_type = 'has_concern' AND deleted_at IS NULL;
            """)
        concern_mems = cur.fetchall()
        concern_sqls = []
        for uid, target_name, season in concern_mems:
            safe_target = re.sub(r"[^a-z0-9_]+", "", target_name.lower())
            if not safe_target:
                continue
            props = f"user_scope: {int(uid)}"
            if season:
                props += f", season: '{safe_str(season)}'"
            concern_sqls.append(
                f"SELECT * FROM cypher('skinmate', $$"
                f"MATCH (u:User {{user_id: {int(uid)}}}), "
                f"      (c:Concern {{name: '{safe_target}'}})"
                f"MERGE (u)-[r:HAS_CONCERN {{{props}}}]->(c)"
                f"$$) AS (result agtype);"
            )
        if concern_sqls:
            cur.execute("\n".join(concern_sqls))

        # 8. 전역 지식이 변경되었으므로 모든 유저의 순회 경로 캐시를 전체 리셋(TRUNCATE)합니다.
        logger.info("invalidating_all_traverse_cache_due_to_global_knowledge_update")
        cur.execute("TRUNCATE TABLE public.traverse_cache;")

    logger.info("finished_graph_global_knowledge_populate")
