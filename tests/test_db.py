"""db.py 통합 테스트 — RLS 스코프가 실제로 사용자 격리를 강제하는지(실 DB 필요).

DB 미기동 시 skip. 근거: AC-M5(격리), 002_memory_and_rls.sql RLS 정책.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from skinmate import db

# 충돌 방지용 테스트 전용 사용자 id
_UID_A = 990001
_UID_B = 990002


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[object]]:
    try:
        c = db.connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"DB 미기동 — 통합테스트 skip: {exc}")
    try:
        yield c
        # 테스트 잔여 행 정리
        with db.user_scope(c, _UID_A):
            c.execute("DELETE FROM memories WHERE user_id = %s", (_UID_A,))
    finally:
        c.close()


def test_no_scope_denies_all(conn: psycopg.Connection[object]) -> None:
    """스코프 미설정 시 memories 는 deny-by-default(0행)."""
    # user_scope 블록 밖에서 직접 트랜잭션 — app.current_user_id 미설정
    with conn.transaction():
        row = conn.execute("SELECT count(*) FROM memories").fetchone()
    assert row is not None
    assert row[0] == 0


def test_scope_isolates_between_users(conn: psycopg.Connection[object]) -> None:
    """A 스코프로 쓴 기억을 B 스코프는 볼 수 없다(정확히 0행)."""
    with db.user_scope(conn, _UID_A):
        conn.execute(
            "INSERT INTO memories (user_id, content, fact_type) VALUES (%s, %s, 'other')",
            (_UID_A, "격리 테스트 사실"),
        )

    # A 는 자기 행을 본다
    with db.user_scope(conn, _UID_A):
        seen_a = conn.execute("SELECT count(*) FROM memories").fetchone()
    assert seen_a is not None and seen_a[0] >= 1

    # B 는 A 의 행을 못 본다
    with db.user_scope(conn, _UID_B):
        seen_b = conn.execute("SELECT count(*) FROM memories").fetchone()
    assert seen_b is not None and seen_b[0] == 0


def test_advisory_lock_runs(conn: psycopg.Connection[object]) -> None:
    """advisory_xact_lock 이 트랜잭션 안에서 오류 없이 실행된다."""
    with db.user_scope(conn, _UID_A):
        db.advisory_xact_lock(conn, _UID_A)
