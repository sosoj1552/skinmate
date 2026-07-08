"""기억 저장소 — memories 테이블의 RLS-스코프 데이터 접근 원시연산.

CRUD 판정(1B.2)·writer(1B.4)가 조합해 쓰는 저수준 함수. 감사로그(memory_audit) 기록은
op 을 아는 상위 계층이 담당한다(여기선 순수 데이터 접근). 모든 함수는 `db.user_scope` 안에서
호출한다 — RLS 로 본인 행만 접근(AC-M5), 미스코프 시 0행/무효과(deny-by-default).
"""

from __future__ import annotations

from typing import Any

import psycopg

from skinmate.contracts.facts import FactType

# 반복 언급(no-op) 시 base_weight 증가분. frequency 와 함께 "자주 언급 상위"를 만든다.
MENTION_WEIGHT_INCREMENT = 1.0


def insert_memory(
    conn: psycopg.Connection[Any],
    *,
    user_id: int,
    content: str,
    fact_type: FactType,
    slot_key: str | None = None,
    season: str | None = None,
    base_weight: float = 1.0,
    target_ingredient_id: int | None = None,
    target_name: str | None = None,
) -> int:
    """새 기억 행 삽입, memory_id 반환. user_id 는 스코프와 일치해야 함(RLS WITH CHECK)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memories
                (user_id, content, fact_type, slot_key, season,
                 base_weight, target_ingredient_id, target_name)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING memory_id
            """,
            (
                user_id,
                content,
                str(fact_type),
                slot_key,
                season,
                base_weight,
                target_ingredient_id,
                target_name,
            ),
        )
        row = cur.fetchone()
    assert row is not None  # RETURNING 은 항상 1행
    return int(row[0])


def bump_mention(conn: psycopg.Connection[Any], memory_id: int) -> None:
    """중복 언급(no-op) 처리: frequency +1, base_weight 증가, last_seen 갱신(가중치 상승)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE memories
            SET frequency = frequency + 1,
                base_weight = base_weight + %s,
                last_seen = now()
            WHERE memory_id = %s AND deleted_at IS NULL
            """,
            (MENTION_WEIGHT_INCREMENT, memory_id),
        )


def soft_delete_memory(conn: psycopg.Connection[Any], memory_id: int) -> None:
    """철회·무효화: 하드 삭제 금지, deleted_at 스탬프만(복구 가능, F1 예외처리)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE memories SET deleted_at = now() WHERE memory_id = %s AND deleted_at IS NULL",
            (memory_id,),
        )
