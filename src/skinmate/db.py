"""DB 접근 계층 — 커넥션 + RLS 스코프 + per-user advisory lock.

memories/memory_audit 는 RLS(002_memory_and_rls.sql)로 격리된다. 앱은 반드시 비-superuser 역할
skinmate_app(004)로 접속하고, 트랜잭션마다 `app.current_user_id` 를 걸어 본인 행만 접근한다.
미설정 시 NULL → 0행(deny-by-default). 그래프 접근은 graph/choke.py 의 age_exec 단일 관문 경유.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg

from skinmate.config import settings


def connect() -> psycopg.Connection[Any]:
    """skinmate_app 역할로 DB 커넥션 생성. autocommit=False(명시적 트랜잭션 제어)."""
    return psycopg.connect(settings.database_url, autocommit=False)


@contextmanager
def user_scope(conn: psycopg.Connection[Any], user_id: int) -> Iterator[None]:
    """트랜잭션을 열고 RLS 스코프(app.current_user_id)를 건다.

    블록 진입 시 트랜잭션 시작 + `set_config(..., is_local=true)` 로 스코프 주입,
    정상 종료 시 COMMIT, 예외 시 ROLLBACK. is_local=true 라 스코프는 트랜잭션 종료와 함께 해제된다.
    memories 조회/쓰기는 반드시 이 블록 안에서 수행한다.
    """
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('app.current_user_id', %s, true)", (str(user_id),))
        yield


def advisory_xact_lock(conn: psycopg.Connection[Any], user_id: int) -> None:
    """user_id 기준 per-user 직렬화 락. 같은 사용자 동시 쓰기를 순서화한다(F5).

    pg_advisory_xact_lock 은 트랜잭션 종료 시 자동 해제되므로 반드시 열린 트랜잭션 안에서 호출한다.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (user_id,))
