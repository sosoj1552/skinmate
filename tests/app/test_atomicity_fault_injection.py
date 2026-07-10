"""원자성 결함주입(WBS 2.4, AC-S1) — process_turn(2.1 배선) 경유로도 부분 저장이 없는지 재확인.

tests/write/test_writer.py 가 write_turn 을 직접 호출해 AGE write 직후 결함 주입 → 관계형/
그래프 부분행 0을 이미 검증했다. 이번엔 응답 생성까지 포함한 전체 배선(process_turn)을 태운
뒤 write 단계에서 실패시켜도 동일하게 롤백되는지, 그리고 예외가 호출자(FastAPI 등)에게
올바르게 전파되는지를 통합 레벨에서 재확인한다(ACCEPTANCE-TESTING.md §3 P0 필수 테스트
test_writer_atomic_rollback.py 의 통합 레벨 대응).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import psycopg
import pytest

from skinmate import db
from skinmate.app.turn import process_turn
from skinmate.graph import choke
from skinmate.memory import bridge, repo

_UID = 990801


class _ScriptedProvider:
    """호출마다 미리 정한 payload 를 순서대로 반환(라우팅→fact추출 2단계 호출 시나리오)."""

    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self._payloads = list(payloads)

    def complete(self, system: str, prompt: str) -> str:
        return ""

    def complete_json(
        self, system: str, prompt: str, schema: dict[str, object]
    ) -> dict[str, object]:
        return self._payloads.pop(0)


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[object]]:
    try:
        c = db.connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"DB 미기동 — 통합테스트 skip: {exc}")
    seeded = bridge.resolve_ingredient(c, "레티놀") is not None
    c.rollback()  # resolve_ingredient 조회로 연 트랜잭션을 정리(아래 user_scope 와 중첩 방지)
    if not seeded:
        pytest.skip("ingredients 시드 없음(scripts/seed_fixtures.py 먼저 실행 필요)")
    try:
        yield c
        with db.user_scope(c, _UID):
            c.execute("DELETE FROM memory_audit WHERE user_id = %s", (_UID,))
            c.execute("DELETE FROM memories WHERE user_id = %s", (_UID,))
            choke.age_exec(c, _UID, "MATCH (u:User {user_id: $user_scope}) DETACH DELETE u", {})
    finally:
        c.close()


def _avoids_retinol(conn: psycopg.Connection[object], uid: int) -> bool:
    with conn.transaction():
        rows = choke.age_exec(
            conn,
            uid,
            "MATCH (u:User {user_id: $user_scope})-[:AVOIDS]->"
            "(i:Ingredient {canonical_key: 'retinol'}) RETURN i.canonical_key AS ck",
            {},
        )
    return len(rows) >= 1


def test_process_turn_atomic_rollback_on_graph_write_failure(
    conn: psycopg.Connection[object],
) -> None:
    """AGE write 직후 결함 주입 → process_turn 을 통째로 태워도 관계형/그래프 부분행 0(AC-S1)."""
    real_project = bridge.project_to_graph

    def project_then_fail(*args: Any, **kwargs: Any) -> None:
        real_project(*args, **kwargs)  # 실제 AGE write 를 먼저 수행(먼저 쓴 상태 재현)
        raise RuntimeError("결함 주입: AGE write 직후 강제 실패")

    provider = _ScriptedProvider(
        [
            {"intent": "statement"},
            {
                "facts": [
                    {
                        "fact_type": "avoid_ingredient",
                        "content": "레티놀 자극나요",
                        "target_name": "레티놀",
                    }
                ]
            },
        ]
    )

    with (
        patch("skinmate.write.writer.bridge.project_to_graph", side_effect=project_then_fail),
        pytest.raises(RuntimeError, match="결함 주입"),
    ):
        process_turn(conn, provider, _UID, "레티놀 쓰면 자극나요")

    # 롤백 확인: 먼저 쓴 관계형 행도, AGE 엣지도 전부 0 잔여(응답 생성까지는 성공했더라도)
    with db.user_scope(conn, _UID):
        assert repo.list_active(conn, _UID) == []
        total = conn.execute("SELECT count(*) FROM memories WHERE user_id=%s", (_UID,)).fetchone()
        assert total is not None and total[0] == 0
    assert not _avoids_retinol(conn, _UID)


def test_process_turn_atomic_rollback_does_not_affect_other_users(
    conn: psycopg.Connection[object],
) -> None:
    """A의 저장 롤백이 B의 기존 데이터에 영향을 주지 않는다(원자성이 지나치게 넓지 않음)."""
    other_uid = _UID + 1
    with db.user_scope(conn, other_uid):
        conn.execute(
            "INSERT INTO memories (user_id, content, fact_type) VALUES (%s, %s, 'other')",
            (other_uid, "기존 사실"),
        )
    conn.commit()

    real_project = bridge.project_to_graph

    def project_then_fail(*args: Any, **kwargs: Any) -> None:
        real_project(*args, **kwargs)
        raise RuntimeError("결함 주입")

    provider = _ScriptedProvider(
        [
            {"intent": "statement"},
            {"facts": [{"fact_type": "skin_type", "content": "지성"}]},
        ]
    )

    with (
        patch("skinmate.write.writer.bridge.project_to_graph", side_effect=project_then_fail),
        pytest.raises(RuntimeError, match="결함 주입"),
    ):
        process_turn(conn, provider, _UID, "저는 지성이에요")

    try:
        with db.user_scope(conn, other_uid):
            assert len(repo.list_active(conn, other_uid)) == 1
    finally:
        with db.user_scope(conn, other_uid):
            conn.execute("DELETE FROM memories WHERE user_id = %s", (other_uid,))
