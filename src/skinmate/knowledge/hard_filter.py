"""회피성분 완전차단 하드필터 모듈 (WBS 1A.5).

사용자가 기피 성분(avoid_ingredient)을 지정한 경우, 해당 성분이 함유된
제품 ID를 추천 후보군에서 0건으로 완전 물리 배제합니다.
"""

from __future__ import annotations

from typing import Any

import psycopg

from skinmate import db


def get_avoided_ingredients_for_user(conn: psycopg.Connection[Any], user_id: int) -> list[int]:
    """사용자가 등록한 기피 성분(avoid_ingredient)의 ID 목록을 조회합니다.

    RLS 세션 격리를 활성화하기 위해 db.user_scope 내에서 쿼리가 실행되어야 합니다.
    """
    with db.user_scope(conn, user_id), conn.cursor() as cur:
        cur.execute("""
                SELECT DISTINCT target_ingredient_id
                FROM memories
                WHERE fact_type = 'avoid_ingredient'
                  AND target_ingredient_id IS NOT NULL
                  AND deleted_at IS NULL;
                """)
        rows = cur.fetchall()
        return [row[0] for row in rows]


def filter_avoided_products(
    conn: psycopg.Connection[Any], user_id: int, product_ids: list[int]
) -> list[int]:
    """주어진 제품 ID 목록 중 사용자의 기피 성분이 함유된 제품을 0건으로 완전 배제합니다.

    기피 성분이 등록되어 있지 않거나 입력 제품 목록이 비어있는 경우, 원본 목록을 그대로 반환합니다.
    """
    if not product_ids:
        return []

    # 1. 사용자의 기피 성분 ID 목록 조회
    avoided_ing_ids = get_avoided_ingredients_for_user(conn, user_id)
    if not avoided_ing_ids:
        return list(product_ids)

    # 2. 기피 성분이 포함된 제품 ID 목록 조회
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT product_id
            FROM product_ingredients
            WHERE ingredient_id = ANY(%s);
            """,
            (avoided_ing_ids,),
        )
        rows = cur.fetchall()
        avoided_prod_ids = {row[0] for row in rows}

    # 3. 입력 제품 목록에서 기피 성분이 포함된 제품을 제외
    filtered = [p_id for p_id in product_ids if p_id not in avoided_prod_ids]
    return filtered
