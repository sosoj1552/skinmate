"""성분 정규화 모듈 normalize.py 에 대한 단위 테스트 (WBS 1A.1)."""

from __future__ import annotations

import psycopg
from ingest.normalize import create_canonical_key, resolve_ingredient_id


def test_create_canonical_key_with_english_name() -> None:
    """영문명이 주어진 경우 영문명 기준 정규화가 수행되는지 검증합니다."""
    # 소문자화 및 특수문자 언더스코어 치환
    key1 = create_canonical_key(
        "Almond/Borage/Linseed/Olive Acids/Glycerides*",
        "(아몬드/보리지/아마/올리브)애씨드/글리세라이즈",
    )
    assert key1 == "almond_borage_linseed_olive_acids_glycerides"

    # 일반적인 복합 성분
    key2 = create_canonical_key("Ascorbyl Glucoside", "아스코빌글루코사이드")
    assert key2 == "ascorbyl_glucoside"


def test_create_canonical_key_with_ko_only() -> None:
    """영문명이 없는 경우 한글명을 기반으로 정규화가 동작하는지 검증합니다."""
    # 괄호 제거 규칙
    key1 = create_canonical_key(None, "소듐하이알루로네이트 (히알루론산)")
    assert key1 == "소듐하이알루로네이트"

    # 단순 한글
    key2 = create_canonical_key(None, "정제수")
    assert key2 == "정제수"

    # 다중 구분자 정제
    key3 = create_canonical_key(None, "돌콩 오일 - 추출물,")
    assert key3 == "돌콩_오일_추출물"


def test_resolve_ingredient_id_reuses_dictionary_entry_by_name_ko(
    db_conn: psycopg.Connection,
) -> None:
    """제품 성분표(한글명만 보유)가 성분사전(영문 canonical_key) 항목을 name_ko 로 찾아
    재사용하는지 검증한다 — canonical_key 만으로 조회하면 실물이 같은 성분인데도 별개 행이
    새로 생성되어 그래프 지식(TREATS 등)과 단절되는 결함(2026-07-10 실데이터 검증 중 발견)."""
    with db_conn.cursor() as cur:
        # 성분사전 크롤(name_en 보유) 결과를 시뮬레이션 — 영문 canonical_key로 적재됨.
        cur.execute(
            """
            INSERT INTO ingredients (canonical_key, name_ko, name_en, grade, intro, source_meta)
            VALUES ('test_retinol_dict', '테스트레티놀', 'Test Retinol', 'Good', '사전 항목', '{}')
            RETURNING ingredient_id;
            """
        )
        row = cur.fetchone()
        assert row is not None
        dict_id = row[0]

        # 제품 성분표 파싱(name_en 없음)은 create_canonical_key(None, name_ko)로 한글
        # canonical_key를 만들어 위 영문 canonical_key와 어긋난다 — name_ko로 재발견해야 한다.
        product_id, product_created = resolve_ingredient_id(cur, name_ko="테스트레티놀")
        assert product_created is False
        assert product_id == dict_id

        new_id, new_created = resolve_ingredient_id(cur, name_ko="테스트신규성분")
        assert new_created is True
        assert new_id != dict_id
