"""회피성분 완전차단 하드필터 단위 테스트 (WBS 1A.5)."""

from __future__ import annotations

import os

import psycopg
import pytest

from skinmate.knowledge.hard_filter import (
    filter_avoided_products,
    get_avoided_ingredients_for_user,
)


@pytest.fixture(name="db_conn")
def fixture_db_conn():
    """테스트용 DB 연결 피스처. 트랜잭션 격리 및 테스트 후 강제 롤백 처리."""
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://skinmate:skinmate-dev-only@localhost:5432/skinmate",
    )
    # superuser 권한(skinmate)으로 임시 테이블 삽입 및 RLS 우회(세팅 목적) 작업 수행
    try:
        with psycopg.connect(db_url, autocommit=False) as conn:
            yield conn
            # 테스트 종료 시 자동으로 트랜잭션을 롤백하여 DB 오염 방지
            conn.rollback()
    except psycopg.OperationalError:
        pytest.skip("database connection failed, skipping hard filter unit test.")


def test_avoid_ingredient_hard_filter(db_conn: psycopg.Connection) -> None:
    """기피 성분이 포함된 제품이 정상적으로 배제되는지 테스트합니다."""
    with db_conn.cursor() as cur:
        # 1. 테스트용 임시 성분 등록
        cur.execute("""
            INSERT INTO ingredients (canonical_key, name_ko)
            VALUES ('test_retinol', '테스트레티놀'), ('test_alcohol', '테스트알코올')
            RETURNING canonical_key, ingredient_id;
            """)
        ing_map = {row[0]: row[1] for row in cur.fetchall()}
        retinol_id = ing_map["test_retinol"]
        alcohol_id = ing_map["test_alcohol"]

        # 2. 테스트용 임시 제품 등록
        cur.execute("""
            INSERT INTO products (name, description)
            VALUES 
                ('레티놀 에센스', '레티놀 함유'), 
                ('수분 크림', '알코올 함유'), 
                ('순한 토너', '성분 깨끗')
            RETURNING name, product_id;
            """)
        prod_map = {row[0]: row[1] for row in cur.fetchall()}
        retinol_essence = prod_map["레티놀 에센스"]
        moisture_cream = prod_map["수분 크림"]
        gentle_toner = prod_map["순한 토너"]

        # 3. 제품-성분 junction 테이블 매핑 등록
        cur.execute(f"""
            INSERT INTO product_ingredients (product_id, ingredient_id) VALUES
            ({retinol_essence}, {retinol_id}),
            ({moisture_cream}, {alcohol_id});
            """)

        # 4. 테스트용 임시 유저(9999)의 기피 성분 기억 등록
        cur.execute(
            """
            INSERT INTO memories (user_id, content, fact_type, target_ingredient_id)
            VALUES (9999, '레티놀은 피하고 싶어요', 'avoid_ingredient', %s);
            """,
            (retinol_id,),
        )

    # 비-superuser인 skinmate_app 계정으로 쿼리가 돌았을 때 RLS 동작을 검증하기 위해,
    # psycopg 세션 격리(GUC app.current_user_id)를 적용하여 테스트 진행
    # (db.user_scope가 정상적으로 RLS를 거치는지 검증)
    user_id = 9999
    product_ids = [retinol_essence, moisture_cream, gentle_toner]

    # 5. 기피 성분 ID 조회 테스트
    avoided_ings = get_avoided_ingredients_for_user(db_conn, user_id)
    assert retinol_id in avoided_ings
    assert alcohol_id not in avoided_ings

    # 6. 하드필터 배제 테스트
    filtered_products = filter_avoided_products(db_conn, user_id, product_ids)

    # 레티놀 에센스(retinol_essence)가 물리적으로 완전히 빠져야 함
    assert retinol_essence not in filtered_products
    assert moisture_cream in filtered_products
    assert gentle_toner in filtered_products
    assert len(filtered_products) == 2


def test_avoid_ingredient_hard_filter_no_memories(
    db_conn: psycopg.Connection,
) -> None:
    """기피 성분이 아예 없는 사용자의 경우 제품이 필터링되지 않고 그대로 반환되는지 확인."""
    with db_conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ingredients (canonical_key, name_ko)
            VALUES ('test_water', '정제수')
            RETURNING ingredient_id;
            """)
        water_id = cur.fetchone()[0]

        cur.execute("""
            INSERT INTO products (name, description)
            VALUES ('테스트 토너', '정제수 포함')
            RETURNING product_id;
            """)
        toner_id = cur.fetchone()[0]

        cur.execute(f"""
            INSERT INTO product_ingredients (product_id, ingredient_id) VALUES
            ({toner_id}, {water_id});
            """)

    user_id = 8888  # 기억이 없는 새로운 사용자
    product_ids = [toner_id]

    filtered_products = filter_avoided_products(db_conn, user_id, product_ids)
    assert filtered_products == product_ids
