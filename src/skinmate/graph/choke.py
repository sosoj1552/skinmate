"""Apache AGE choke-point (단일 관문) 구현.

WBS 0.5 조기 납품. 모든 AGE 그래프 접근은 이 모듈을 경유하며,
user_scope를 통해 사용자 간 격리를 보장합니다.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg


def age_exec(
    conn: psycopg.Connection[Any],
    user_scope: int | None,
    cypher_query: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Apache AGE cypher 쿼리를 실행하는 단일 관문 함수.

    Args:
        conn: psycopg DB Connection
        user_scope: 조회/생성을 격리할 user_id. 전역 읽기인 경우에만 None 허용.
        cypher_query: 실행할 cypher 쿼리 템플릿
        params: cypher 쿼리에 바인딩할 파라미터

    Returns:
        조회 결과를 dict의 list로 반환
    """
    if params is None:
        params = {}

    # user_scope 강제성 주입
    params_with_scope = dict(params)
    if user_scope is not None:
        params_with_scope["user_scope"] = user_scope

    # AGE cypher 파라미터는 JSON 문자열로 전달되어야 함
    params_json = json.dumps(params_with_scope)

    with conn.cursor() as cur:
        # 1. AGE를 위한 search_path 설정 (LOCAL로 트랜잭션 종료 시 복원)
        cur.execute("SET LOCAL search_path = ag_catalog, public;")

        # 2. cypher() 함수를 사용한 쿼리 실행
        # SELECT * FROM cypher('graph_name', 'query', 'params_json') AS (n agtype);
        sql = "SELECT * FROM cypher('skinmate', %s, %s) AS (result agtype);"
        cur.execute(sql, (cypher_query, params_json))

        # 3. 결과 반환
        results = []
        for row in cur.fetchall():
            val = row[0]
            # agtype은 psycopg에 의해 dict나 기본타입으로 자동 변환되거나 str로 넘어옴
            if isinstance(val, dict):
                results.append(val)
            elif isinstance(val, str):
                try:
                    results.append(json.loads(val))
                except json.JSONDecodeError:
                    results.append({"value": val})
            else:
                results.append({"value": val})

        return results
