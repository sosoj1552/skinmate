"""기억 저장소 — memories 테이블의 RLS-스코프 데이터 접근 원시연산.

CRUD 판정(1B.2)·writer(1B.4)가 조합해 쓰는 저수준 함수. 감사로그(memory_audit) 기록은
op 을 아는 상위 계층이 담당한다(여기선 순수 데이터 접근). 모든 함수는 `db.user_scope` 안에서
호출한다 — RLS 로 본인 행만 접근(AC-M5), 미스코프 시 0행/무효과(deny-by-default).
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from skinmate.contracts.facts import FactType

# 반복 언급(no-op) 시 base_weight 증가분. frequency 와 함께 "자주 언급 상위"를 만든다.
MENTION_WEIGHT_INCREMENT = 1.0


class ActiveMemory(BaseModel):
    """활성(미삭제) 기억 행의 CRUD 판정용 최소 뷰(1B.2 judge 입력)."""

    memory_id: int
    fact_type: FactType
    slot_key: str | None
    target_name: str | None
    content: str
    season: str | None


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


def list_active(conn: psycopg.Connection[Any], user_id: int) -> list[ActiveMemory]:
    """user 의 활성 기억을 CRUD 판정용 최소 뷰로 반환. user_scope 안에서 호출(RLS)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT memory_id, fact_type, slot_key, target_name, content, season
            FROM memories
            WHERE user_id = %s AND deleted_at IS NULL
            """,
            (user_id,),
        )
        rows = cur.fetchall()
    return [
        ActiveMemory(
            memory_id=r[0],
            fact_type=FactType(r[1]),
            slot_key=r[2],
            target_name=r[3],
            content=r[4],
            season=r[5],
        )
        for r in rows
    ]


def update_memory(
    conn: psycopg.Connection[Any],
    memory_id: int,
    *,
    content: str,
    fact_type: FactType,
    slot_key: str | None = None,
    season: str | None = None,
    target_name: str | None = None,
    target_ingredient_id: int | None = None,
) -> None:
    """같은 슬롯의 값 전환(update): 최신값으로 덮고 재언급으로 취급(가중치·빈도 상승)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE memories
            SET content = %s, fact_type = %s, slot_key = %s, season = %s, target_name = %s,
                target_ingredient_id = %s,
                base_weight = base_weight + %s, frequency = frequency + 1, last_seen = now()
            WHERE memory_id = %s AND deleted_at IS NULL
            """,
            (
                content,
                str(fact_type),
                slot_key,
                season,
                target_name,
                target_ingredient_id,
                MENTION_WEIGHT_INCREMENT,
                memory_id,
            ),
        )


def insert_audit(
    conn: psycopg.Connection[Any],
    *,
    user_id: int,
    memory_id: int | None,
    op: str,
    old_val: dict[str, Any] | None,
    new_val: dict[str, Any] | None,
) -> None:
    """CRUD 감사로그 1행. delete 는 하드삭제 대신 이 로그로 추적(AC-M1). user_scope 안에서 호출."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO memory_audit (user_id, memory_id, op, old_val, new_val)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                user_id,
                memory_id,
                op,
                Jsonb(old_val) if old_val is not None else None,
                Jsonb(new_val) if new_val is not None else None,
            ),
        )
