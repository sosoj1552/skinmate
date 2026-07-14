"""성분명 정규화 및 INCI 매핑 유틸리티 (WBS 1A.1)."""

from __future__ import annotations

import json
import re
from typing import Any

import psycopg


def create_canonical_key(english_name: str | None, name_ko: str) -> str:
    """성분 영문명 혹은 한글명을 기준으로 정규화된 canonical_key를 생성합니다.

    규칙:
    - 소문자화
    - 끝의 * 등 비-단어 특수문자 제거
    - 공백, 하이픈 등 구분자는 언더스코어(_)로 단일화
    - 양끝의 언더스코어 제거
    """
    if english_name:
        key = english_name.lower().strip()
        # 끝에 붙은 별표(*) 등 특수문자 제거
        key = re.sub(r"\*+$", "", key)
        # 알파벳, 숫자, 언더스코어를 제외한 문자를 언더스코어로 변환
        key = re.sub(r"[^a-z0-9_]+", "_", key)
        key = key.strip("_")
        if key:
            return key

    # 한글명 정규화 폴백
    key = name_ko.strip()
    # 괄호 및 괄호 내부 내용 제거
    key = re.sub(r"\s*\(.*?\)\s*", "", key)
    # 한글, 알파벳, 숫자 이외의 문자를 언더스코어로 변환
    key = re.sub(r"[^가-힣a-zA-Z0-9]+", "_", key)
    key = key.strip("_")
    return key


def resolve_ingredient_id(
    cur: psycopg.Cursor[Any],
    *,
    name_en: str | None = None,
    name_ko: str,
    default_grade: str = "Good",
    default_intro: str = "제품 성분표 기반 자동 등록",
    default_source_meta: dict[str, Any] | None = None,
) -> tuple[int, bool]:
    """성분을 조회하거나 없으면 생성한다. 이미 있는 성분사전 항목(다른 canonical_key 라도
    name_ko 가 같은 행)을 우선 재사용해 같은 실물 성분이 중복 행으로 쪼개지지 않게 한다.

    배경: 제품 성분표는 name_en 없이 한글명만 갖고 있어 canonical_key 가 한글로 만들어지는데,
    성분사전(name_en 보유, TREATS/AGGRAVATES 지식 보유)은 영문 canonical_key 를 쓴다. 둘 다
    canonical_key 로만 조회하면 같은 실물 성분이 두 개의 분리된 행으로 쪼개져(실측 390개 중
    388개, 92%가 사전에 동일 name_ko 를 가진 영문 항목과 중복), 그래프 2-hop 추론(TREATS 경로)이
    실데이터에서 항상 0건을 반환했다.

    조회 순서:
      1. canonical_key 정확히 일치(name_en 이 있는 정상 경로에서 주로 히트)
      2. name_ko 일치(제품 성분표처럼 canonical_key 가 한글로 생성된 경우, 이미 성분사전에
         등록된 영문-키 행을 이걸로 찾아낸다 — 핵심 수정 지점)
      3. 위 둘 다 없으면 새로 생성

    반환: (ingredient_id, created 여부)
    """
    canonical_key = create_canonical_key(name_en, name_ko)

    cur.execute("SELECT ingredient_id FROM ingredients WHERE canonical_key = %s;", (canonical_key,))
    row = cur.fetchone()
    if row:
        return row[0], False

    cur.execute("SELECT ingredient_id FROM ingredients WHERE name_ko = %s LIMIT 1;", (name_ko,))
    row = cur.fetchone()
    if row:
        return row[0], False

    cur.execute(
        """
        INSERT INTO ingredients (canonical_key, name_ko, grade, intro, source_meta)
        VALUES (%s, %s, %s, %s, %s)
        RETURNING ingredient_id;
        """,
        (
            canonical_key,
            name_ko,
            default_grade,
            default_intro,
            json.dumps(default_source_meta or {}),
        ),
    )
    res_row = cur.fetchone()
    assert res_row is not None
    return res_row[0], True
