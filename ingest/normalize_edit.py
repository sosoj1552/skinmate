"""normalize.py 수정 제안본 — 원본은 건드리지 않는다. 작업자A 검토용, 채택 시 normalize.py 를
이 파일 내용으로 교체하고 이 파일은 폐기하는 방식으로 반영 예정.

발견한 문제(2026-07-10, 실데이터 검증 중):
  crawler.py 가 성분을 두 경로로 적재하는데, 서로 다른 canonical_key 를 만들어내
  같은 실물 성분이 두 개의 분리된 행으로 쪼개진다.
    - 성분사전 페이지(라인 144): create_canonical_key(name_en, name_ko) → 영문 key
      (예: canonical_key='retinol'). TREATS/AGGRAVATES 그래프 지식이 여기 붙는다.
    - 제품 페이지 성분표(라인 308): create_canonical_key(None, raw_ing) → 한글 key
      (예: canonical_key='레티놀'). 이후 canonical_key 로만 기존 성분을 조회하므로
      영문 사전 행을 못 찾고 지식이 없는 새 행을 또 만든다.
  실측 결과: 제품에 실제 연결된 고유 성분 390개 중 388개(99.5%)가 한글 key, 그 중
  358개(92%)가 성분사전에 동일 name_ko 를 가진 영문 항목과 중복. 즉 "제품에 든 성분"과
  "효능을 아는 성분"이 대부분 서로 다른 행이라 2-hop 그래프 추론(TREATS 경로)이 실데이터에서
  거의 항상 0건을 반환한다.

수정 방향:
  제품 성분표에서 성분을 조회/생성할 때, canonical_key 뿐 아니라 name_ko 로도 기존 사전
  항목을 찾아 재사용한다 — 새 함수 resolve_ingredient_id() 로 그 규칙을 캡슐화한다.
  crawler.py 라인 310~347 이 이 함수를 쓰도록 바뀌어야 한다(원본 파일은 여기서 안 바꿈).
"""

from __future__ import annotations

import json
import re
from typing import Any

import psycopg


def create_canonical_key(english_name: str | None, name_ko: str) -> str:
    """성분 영문명 혹은 한글명을 기준으로 정규화된 canonical_key를 생성합니다.

    규칙(원본과 동일, 변경 없음):
    - 소문자화
    - 끝의 * 등 비-단어 특수문자 제거
    - 공백, 하이픈 등 구분자는 언더스코어(_)로 단일화
    - 양끝의 언더스코어 제거
    """
    if english_name:
        key = english_name.lower().strip()
        key = re.sub(r"\*+$", "", key)
        key = re.sub(r"[^a-z0-9_]+", "_", key)
        key = key.strip("_")
        if key:
            return key

    key = name_ko.strip()
    key = re.sub(r"\s*\(.*?\)\s*", "", key)
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

    조회 순서:
      1. canonical_key 정확히 일치(기존 로직과 동일, name_en 이 있는 정상 경로에서 주로 히트)
      2. name_ko 일치(제품 성분표처럼 name_en 이 없어 canonical_key 가 한글로 생성된 경우,
         이미 성분사전에 등록된 영문-키 행을 이걸로 찾아낸다 — 핵심 수정 지점)
      3. 위 둘 다 없으면 새로 생성(기존 로직과 동일)

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
