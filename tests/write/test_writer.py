"""writer.write_turn 통합 테스트 — 표+그래프 원자 저장(AC-S1/S2), 비파괴 공존, 다리 연동.

전부 실 DB(관계형 RLS + AGE) 통합. LLM 은 가짜/스크립트 프로바이더로 대체(비용·재현성).
seed_fixtures.py 의 ingredients/Concern 시드를 전제로 한다. DB·시드 미기동 시 skip.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import psycopg
import pytest

from skinmate import db
from skinmate.graph import choke
from skinmate.memory import bridge, repo
from skinmate.write.writer import write_turn

_UID = 990501
_UID_B = 990502


class _CannedProvider:
    """항상 같은 dict 를 반환하는 가짜 프로바이더."""

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def complete(self, system: str, prompt: str) -> str:
        return ""

    def complete_json(
        self, system: str, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        return self._payload


def _fact_payload(
    fact_type: str, target_name: str, *, retract: bool = False, season: str | None = None
) -> dict[str, Any]:
    return {
        "facts": [
            {
                "fact_type": fact_type,
                "content": f"{target_name} 관련",
                "target_name": target_name,
                "retract": retract,
                "season": season,
            }
        ]
    }


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[object]]:
    try:
        c = db.connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"DB 미기동 — 통합테스트 skip: {exc}")
    # 자체 트랜잭션으로 감싸 커밋/종료 — write_turn 의 db.user_scope 가 각 호출마다 최상위
    # 트랜잭션이 되도록 커넥션을 깨끗한 상태로 넘긴다(안 그러면 세이브포인트로 중첩되어
    # AC-S2 가 검증하려는 실제 커밋이 발생하지 않는다).
    with c.transaction():
        seeded = bridge.resolve_ingredient(c, "레티놀") is not None
    if not seeded:
        pytest.skip("ingredients 시드 없음(scripts/seed_fixtures.py 먼저 실행 필요)")
    try:
        yield c
        for uid in (_UID, _UID_B):
            with db.user_scope(c, uid):
                c.execute("DELETE FROM memory_audit WHERE user_id = %s", (uid,))
                c.execute("DELETE FROM memories WHERE user_id = %s", (uid,))
                choke.age_exec(c, uid, "MATCH (u:User {user_id: $user_scope}) DETACH DELETE u", {})
    finally:
        c.close()


def _avoids_retinol(conn: psycopg.Connection[object], uid: int) -> bool:
    # 자체 트랜잭션으로 감싸 즉시 커밋/종료 — 이 확인 호출이 다음 write_turn 의 db.user_scope
    # 를 세이브포인트로 중첩시키지 않도록(연결을 항상 idle 상태로 되돌린다).
    with conn.transaction():
        rows = choke.age_exec(
            conn,
            uid,
            "MATCH (u:User {user_id: $user_scope})-[:AVOIDS]->"
            "(i:Ingredient {canonical_key: 'retinol'}) RETURN i.canonical_key AS ck",
            {},
        )
    return len(rows) == 1


def _prefers_retinol(conn: psycopg.Connection[object], uid: int) -> bool:
    with conn.transaction():
        rows = choke.age_exec(
            conn,
            uid,
            "MATCH (u:User {user_id: $user_scope})-[:PREFERS]->"
            "(i:Ingredient {canonical_key: 'retinol'}) RETURN i.canonical_key AS ck",
            {},
        )
    return len(rows) == 1


def test_write_turn_smalltalk_is_noop(conn: psycopg.Connection[object]) -> None:
    provider = _CannedProvider({"facts": []})
    decisions = write_turn(conn, provider, _UID, "오늘 피곤해")
    assert decisions == []
    with db.user_scope(conn, _UID):
        assert repo.list_active(conn, _UID) == []


def test_write_turn_add_creates_row_and_graph_edge(conn: psycopg.Connection[object]) -> None:
    provider = _CannedProvider(_fact_payload("avoid_ingredient", "레티놀"))
    decisions = write_turn(conn, provider, _UID, "레티놀 쓰면 자극나요")

    assert len(decisions) == 1
    with db.user_scope(conn, _UID):
        active = repo.list_active(conn, _UID)
        assert len(active) == 1
        assert active[0].fact_type.value == "avoid_ingredient"
        # target_ingredient_id 가 bridge.resolve_ingredient 로 해석되어 FK 로 채워졌는지
        row = conn.execute(
            "SELECT target_ingredient_id FROM memories WHERE user_id=%s AND deleted_at IS NULL",
            (_UID,),
        ).fetchone()
        assert row is not None and row[0] is not None
        assert _avoids_retinol(conn, _UID)


def test_write_turn_update_flips_graph_edge(conn: psycopg.Connection[object]) -> None:
    p1 = _CannedProvider(_fact_payload("avoid_ingredient", "레티놀"))
    write_turn(conn, p1, _UID, "레티놀 자극나요")
    assert _avoids_retinol(conn, _UID)

    p2 = _CannedProvider(_fact_payload("prefer_ingredient", "레티놀"))
    write_turn(conn, p2, _UID, "레티놀 이제 좋아요")

    assert not _avoids_retinol(conn, _UID)
    assert _prefers_retinol(conn, _UID)
    with db.user_scope(conn, _UID):
        active = repo.list_active(conn, _UID)
        assert len(active) == 1
        assert active[0].fact_type.value == "prefer_ingredient"


def test_write_turn_retract_soft_deletes_and_removes_edge(conn: psycopg.Connection[object]) -> None:
    p1 = _CannedProvider(_fact_payload("avoid_ingredient", "레티놀"))
    write_turn(conn, p1, _UID, "레티놀 자극나요")
    assert _avoids_retinol(conn, _UID)

    p2 = _CannedProvider(_fact_payload("avoid_ingredient", "레티놀", retract=True))
    write_turn(conn, p2, _UID, "이제 레티놀 괜찮아요")

    assert not _avoids_retinol(conn, _UID)
    with db.user_scope(conn, _UID):
        assert repo.list_active(conn, _UID) == []
        total = conn.execute("SELECT count(*) FROM memories WHERE user_id=%s", (_UID,)).fetchone()
        assert total is not None and total[0] == 1  # soft-delete, 행은 남음


def test_write_turn_unresolvable_ingredient_saves_text_skips_graph(
    conn: psycopg.Connection[object],
) -> None:
    """PRD F1 예외: 성분 해석 실패해도 텍스트는 저장, 그래프 엣지만 skip."""
    provider = _CannedProvider(_fact_payload("avoid_ingredient", "존재하지않는성분XYZ"))
    decisions = write_turn(conn, provider, _UID, "존재하지않는성분XYZ 별로예요")

    assert len(decisions) == 1
    with db.user_scope(conn, _UID):
        active = repo.list_active(conn, _UID)
        assert len(active) == 1
        assert active[0].target_name == "존재하지않는성분XYZ"
        row = conn.execute(
            "SELECT target_ingredient_id FROM memories WHERE user_id=%s AND deleted_at IS NULL",
            (_UID,),
        ).fetchone()
        assert row is not None and row[0] is None


def test_write_turn_isolation_between_users(conn: psycopg.Connection[object]) -> None:
    provider = _CannedProvider(_fact_payload("avoid_ingredient", "레티놀"))
    write_turn(conn, provider, _UID, "레티놀 자극나요")

    with db.user_scope(conn, _UID_B):
        assert repo.list_active(conn, _UID_B) == []
    assert not _avoids_retinol(conn, _UID_B)


# ── AC-S1: 원자 단일-tx — AGE write 뒤 커밋 전 결함 주입 → 관계형/그래프 부분행 0 ──
def test_write_turn_atomic_rollback_on_graph_write_failure(
    conn: psycopg.Connection[object],
) -> None:
    real_project = bridge.project_to_graph

    def project_then_fail(*args: Any, **kwargs: Any) -> None:
        real_project(*args, **kwargs)  # 실제 AGE write 를 먼저 수행(먼저 쓴 상태 재현)
        raise RuntimeError("결함 주입: AGE write 직후 강제 실패")

    provider = _CannedProvider(_fact_payload("avoid_ingredient", "레티놀"))
    with (
        patch("skinmate.write.writer.bridge.project_to_graph", side_effect=project_then_fail),
        pytest.raises(RuntimeError, match="결함 주입"),
    ):
        write_turn(conn, provider, _UID, "레티놀 자극나요")

    # 롤백 확인: 먼저 쓴 관계형 행도, AGE 엣지도 전부 0 잔여
    with db.user_scope(conn, _UID):
        assert repo.list_active(conn, _UID) == []
        total = conn.execute("SELECT count(*) FROM memories WHERE user_id=%s", (_UID,)).fetchone()
        assert total is not None and total[0] == 0
    assert not _avoids_retinol(conn, _UID)


def test_write_turn_atomic_rollback_multi_fact(conn: psycopg.Connection[object]) -> None:
    """한 턴에 사실 2개 — 두번째 것의 그래프 write 에서 실패해도 첫번째 것까지 전부 롤백."""
    real_project = bridge.project_to_graph
    call_count = {"n": 0}

    def fail_on_second(*args: Any, **kwargs: Any) -> None:
        call_count["n"] += 1
        real_project(*args, **kwargs)
        if call_count["n"] == 2:
            raise RuntimeError("결함 주입: 두번째 그래프 write 실패")

    provider = _CannedProvider(
        {
            "facts": [
                {
                    "fact_type": "avoid_ingredient",
                    "content": "레티놀 회피",
                    "target_name": "레티놀",
                },
                {"fact_type": "has_concern", "content": "건조 고민", "target_name": "건조"},
            ]
        }
    )
    with (
        patch("skinmate.write.writer.bridge.project_to_graph", side_effect=fail_on_second),
        pytest.raises(RuntimeError, match="결함 주입"),
    ):
        write_turn(conn, provider, _UID, "레티놀 자극나고 건조해요")

    with db.user_scope(conn, _UID):
        assert repo.list_active(conn, _UID) == []  # 먼저 쓴 avoid_ingredient 행도 롤백됨
    assert not _avoids_retinol(conn, _UID)


# ── AC-S2: 동기 가시성 — 턴 반환 전 커밋, 새 커넥션에서도 즉시 보임(drain 없음) ──
def test_write_turn_commits_before_return_visible_on_new_connection(
    conn: psycopg.Connection[object],
) -> None:
    provider = _CannedProvider(_fact_payload("avoid_ingredient", "레티놀"))
    write_turn(conn, provider, _UID, "레티놀 자극나요")

    other_conn = db.connect()
    try:
        with db.user_scope(other_conn, _UID):
            active = repo.list_active(other_conn, _UID)
        assert len(active) == 1
        assert active[0].target_name == "레티놀"
    finally:
        other_conn.close()
