"""rank_memory / effective_weight 테스트 — AC-M2(가중치 순위), AC-M5(격리).

effective_weight 공식은 순수 유닛으로, 순위·격리는 실 DB(RLS) 통합으로 검증한다.
DB 미기동 시 통합 케이스만 skip.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from skinmate import db
from skinmate.memory.rank import LAMBDA_PER_DAY, effective_weight, rank_memory

_UID_A = 990101
_UID_B = 990102
_FIXTURE = Path(__file__).resolve().parents[2] / "eval" / "fixtures" / "weight_fixture.json"


# ── 순수 유닛: 시간감쇠 공식 (DB 불필요) ───────────────────────────────
def test_effective_weight_half_life() -> None:
    """λ=0.05/day → 14일 경과 시 exp(-0.7)≈0.497 배."""
    now = datetime(2026, 7, 8, tzinfo=UTC)
    w = effective_weight(1.0, now - timedelta(days=14), now)
    assert w == pytest.approx(math.exp(-LAMBDA_PER_DAY * 14))


def test_effective_weight_future_clamped() -> None:
    """미래 last_seen(시계 오차)은 Δdays=0 으로 클램프 → base_weight 그대로."""
    now = datetime(2026, 7, 8, tzinfo=UTC)
    assert effective_weight(1.5, now + timedelta(days=3), now) == pytest.approx(1.5)


# ── 통합: 순위·격리 (실 DB + RLS) ─────────────────────────────────────
@pytest.fixture
def conn() -> Iterator[psycopg.Connection[object]]:
    try:
        c = db.connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"DB 미기동 — 통합테스트 skip: {exc}")
    try:
        yield c
        for uid in (_UID_A, _UID_B):
            with db.user_scope(c, uid):
                c.execute("DELETE FROM memories WHERE user_id = %s", (uid,))
    finally:
        c.close()


def _seed_from_fixture(conn: psycopg.Connection[object], user_id: int) -> datetime:
    """weight_fixture 를 지정 사용자로 시드하고 기준 now 를 반환."""
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    now = datetime.fromisoformat(data["now"])
    with db.user_scope(conn, user_id):
        for m in data["memories"]:
            last_seen = now - timedelta(days=m["days_ago"])
            conn.execute(
                """
                INSERT INTO memories (user_id, content, fact_type, base_weight, last_seen)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (user_id, m["content"], m["fact_type"], m["base_weight"], last_seen),
            )
    return now


def test_rank_orders_by_effective_weight(conn: psycopg.Connection[object]) -> None:
    """AC-M2: seeded fixture 의 effective_weight 순서와 rank_memory 순위가 일치."""
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    now = _seed_from_fixture(conn, _UID_A)

    with db.user_scope(conn, _UID_A):
        ranked = rank_memory(conn, _UID_A, now=now)

    assert [f.content for f in ranked] == data["expected_order"]
    # effective_weight 는 단조 감소(내림차순)
    weights = [f.effective_weight for f in ranked]
    assert weights == sorted(weights, reverse=True)


def test_rank_isolation_cross_user(conn: psycopg.Connection[object]) -> None:
    """AC-M5: A 로 시드한 기억을 B 스코프의 rank_memory 는 0건으로 본다."""
    now = _seed_from_fixture(conn, _UID_A)

    with db.user_scope(conn, _UID_B):
        ranked_b = rank_memory(conn, _UID_B, now=now)

    assert ranked_b == []
