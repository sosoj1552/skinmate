"""회피 성분 무작위 스위트(WBS 2.6, AC-R2) — 통합 검색 창구에서 회피 성분 제품이 0건인지.

ACCEPTANCE-TESTING.md §3 P0 필수 테스트 test_hard_filter_zero.py 의 통합 레벨 대응.
knowledge/hard_filter.py(1A.5)가 이미 단위 레벨로 검증했지만, retrieve.py(1A.7)는 별도의
인라인 하드필터 SQL을 쓰므로 실제 사용자가 타는 retrieve_recommendation_context 경로에서도
회피 성분이 새지 않는지 여러 사용자·여러 질의 조합(고정 시드 무작위)으로 재확인한다.
"""

from __future__ import annotations

import random
from typing import Any

import psycopg

from skinmate.contracts.facts import FactType
from skinmate.documents.embed import embed_text
from skinmate.memory import bridge, crud
from skinmate.memory.crud import CrudDecision, CrudOp
from skinmate.memory.extract import ExtractedFact
from skinmate.retrieval.retrieve import retrieve_recommendation_context

_BASE_UID = 995000
_INGREDIENTS = ["avoid_a", "avoid_b", "avoid_c", "avoid_d"]
_QUERIES = [
    "보습 잘 되는 제품 추천해줘",
    "트러블 진정에 좋은 화장품",
    "가볍고 산뜻한 제형 원해요",
    "주름 개선에 도움되는 제품",
    "민감성 피부에 순한 제품",
]


def _seed_pool(conn: psycopg.Connection[Any]) -> dict[str, dict[str, int]]:
    """성분 4종 + 제품 8종(각 제품은 성분 1~2개 포함, 겹치도록)을 시드한다."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ingredients (canonical_key, name_ko)
            VALUES ('avoid_a', '테스트회피A'), ('avoid_b', '테스트회피B'),
                   ('avoid_c', '테스트회피C'), ('avoid_d', '테스트회피D')
            ON CONFLICT (canonical_key) DO UPDATE SET name_ko = EXCLUDED.name_ko
            RETURNING canonical_key, ingredient_id;
            """)
        ing_map = {row[0]: row[1] for row in cur.fetchall()}

        product_rows = []
        for i in range(8):
            desc = _QUERIES[i % len(_QUERIES)]
            vector = embed_text(desc)
            product_rows.append((f"테스트 무작위스위트 제품{i}", desc, vector, "bge-m3"))
        cur.execute(
            "INSERT INTO products (name, description, embedding, embedding_model_id) VALUES "
            + ",".join(["(%s, %s, %s, %s)"] * len(product_rows))
            + " RETURNING name, product_id;",
            [v for row in product_rows for v in row],
        )
        prod_map = {row[0]: row[1] for row in cur.fetchall()}
        product_ids = list(prod_map.values())

        rng = random.Random(42)  # 고정 시드 — 재현 가능한 무작위 구성
        junction_rows = []
        for pid in product_ids:
            n_ing = rng.randint(1, 2)
            chosen = rng.sample(_INGREDIENTS, n_ing)
            for key in chosen:
                junction_rows.append((pid, ing_map[key]))
        cur.executemany(
            "INSERT INTO product_ingredients (product_id, ingredient_id) VALUES (%s, %s)",
            junction_rows,
        )

    return {"ingredients": ing_map, "products": prod_map}


def test_random_suite_never_recommends_avoided_ingredient_products(
    db_conn: psycopg.Connection,
) -> None:
    """20건의 (임의 사용자 x 임의 회피성분 x 임의 질의) 조합에서 회피 성분 제품 0건(AC-R2)."""
    pool = _seed_pool(db_conn)
    rng = random.Random(7)  # 고정 시드 — 재현 가능한 무작위 스위트

    violations: list[str] = []
    for trial in range(20):
        uid = _BASE_UID + trial
        avoided_key = rng.choice(_INGREDIENTS)
        query = rng.choice(_QUERIES)

        fact = ExtractedFact(
            fact_type=FactType.AVOID_INGREDIENT,
            content=f"{avoided_key} 자극나요",
            target_name=avoided_key,
        )
        decision = CrudDecision(op=CrudOp.ADD, fact=fact)
        crud.apply_decision(
            db_conn, uid, decision, target_ingredient_id=pool["ingredients"][avoided_key]
        )
        bridge.project_to_graph(db_conn, uid, decision, ingredient_key=avoided_key)

        context = retrieve_recommendation_context(db_conn, user_id=uid, query=query, limit=50)

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT product_id FROM product_ingredients WHERE ingredient_id = %s",
                (pool["ingredients"][avoided_key],),
            )
            avoided_product_ids = {row[0] for row in cur.fetchall()}

        returned_ids = {p.product_id for p in context.products}
        leaked = returned_ids & avoided_product_ids
        if leaked:
            violations.append(
                f"trial={trial} uid={uid} avoided={avoided_key} leaked_products={leaked}"
            )

    assert not violations, "회피 성분 제품이 추천에 새어나감:\n" + "\n".join(violations)
