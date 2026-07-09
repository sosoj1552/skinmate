"""기억→그래프 다리(1B.5) 테스트 — 성분/고민 해석 + 개인 엣지 투영(add/update/delete).

전부 실 DB(관계형+AGE) 통합 테스트. seed_fixtures.py 로 심어진 ingredients/Concern 을 전제로
한다(레티놀→retinol, 히알루론산→hyaluronic_acid, Concern{label:'건조', name:'dryness'}).
DB 미기동 또는 시드 미적재 시 skip.
"""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
import pytest

from skinmate import db
from skinmate.contracts.facts import FactType
from skinmate.graph import choke
from skinmate.memory import bridge
from skinmate.memory.crud import CrudDecision, CrudOp
from skinmate.memory.extract import ExtractedFact
from skinmate.memory.repo import ActiveMemory

_UID = 990401


@pytest.fixture
def conn() -> Iterator[psycopg.Connection[object]]:
    try:
        c = db.connect()
    except psycopg.OperationalError as exc:
        pytest.skip(f"DB 미기동 — 통합테스트 skip: {exc}")
    try:
        yield c
        with db.user_scope(c, _UID):
            choke.age_exec(c, _UID, "MATCH (u:User {user_id: $user_scope}) DETACH DELETE u", {})
    finally:
        c.close()


def _require_seed(conn: psycopg.Connection[object]) -> None:
    # 자체 트랜잭션으로 감싸 커밋/종료 — 이후 테스트 본문의 db.user_scope 가 최상위 트랜잭션이
    # 되도록 커넥션을 깨끗한 상태로 되돌린다(안 그러면 세이브포인트로 중첩되어 실커밋이 안 됨).
    with conn.transaction():
        resolved = bridge.resolve_ingredient(conn, "레티놀")
    if resolved is None:
        pytest.skip("ingredients 시드 없음(scripts/seed_fixtures.py 먼저 실행 필요)")


def test_resolve_ingredient_found(conn: psycopg.Connection[object]) -> None:
    _require_seed(conn)
    resolved = bridge.resolve_ingredient(conn, "레티놀")
    assert resolved is not None
    ingredient_id, canonical_key = resolved
    assert canonical_key == "retinol"
    assert ingredient_id > 0


def test_resolve_ingredient_not_found(conn: psycopg.Connection[object]) -> None:
    assert bridge.resolve_ingredient(conn, "존재하지않는성분XYZ") is None


def test_resolve_concern_key_found(conn: psycopg.Connection[object]) -> None:
    with db.user_scope(conn, _UID):
        key = bridge.resolve_concern_key(conn, "건조")
    assert key == "dryness"


def test_resolve_concern_key_not_found(conn: psycopg.Connection[object]) -> None:
    with db.user_scope(conn, _UID):
        assert bridge.resolve_concern_key(conn, "존재하지않는고민XYZ") is None


def _avoids(conn: psycopg.Connection[object], user_id: int, canonical_key: str) -> bool:
    rows = choke.age_exec(
        conn,
        user_id,
        "MATCH (u:User {user_id: $user_scope})-[:AVOIDS]->(i:Ingredient {canonical_key: $key}) "
        "RETURN i.canonical_key AS ck",
        {"key": canonical_key},
    )
    return len(rows) == 1


def _prefers(conn: psycopg.Connection[object], user_id: int, canonical_key: str) -> bool:
    rows = choke.age_exec(
        conn,
        user_id,
        "MATCH (u:User {user_id: $user_scope})-[:PREFERS]->(i:Ingredient {canonical_key: $key}) "
        "RETURN i.canonical_key AS ck",
        {"key": canonical_key},
    )
    return len(rows) == 1


def test_project_to_graph_add_creates_avoids_edge(conn: psycopg.Connection[object]) -> None:
    _require_seed(conn)
    fact = ExtractedFact(
        fact_type=FactType.AVOID_INGREDIENT, content="레티놀 회피", target_name="레티놀"
    )
    decision = CrudDecision(op=CrudOp.ADD, fact=fact, slot_key="ingredient:레티놀")
    with db.user_scope(conn, _UID):
        bridge.project_to_graph(conn, _UID, decision, ingredient_key="retinol")
        assert _avoids(conn, _UID, "retinol")


def test_project_to_graph_update_switches_edge_type(conn: psycopg.Connection[object]) -> None:
    """avoid→prefer 전환 시 AVOIDS 제거 + PREFERS 생성."""
    _require_seed(conn)
    existing = ActiveMemory(
        memory_id=1,
        fact_type=FactType.AVOID_INGREDIENT,
        slot_key="ingredient:레티놀",
        target_name="레티놀",
        content="레티놀 회피",
        season=None,
    )
    new_fact = ExtractedFact(
        fact_type=FactType.PREFER_INGREDIENT, content="레티놀 선호", target_name="레티놀"
    )
    with db.user_scope(conn, _UID):
        add_decision = CrudDecision(
            op=CrudOp.ADD,
            fact=ExtractedFact(
                fact_type=FactType.AVOID_INGREDIENT, content="레티놀 회피", target_name="레티놀"
            ),
            slot_key="ingredient:레티놀",
        )
        bridge.project_to_graph(conn, _UID, add_decision, ingredient_key="retinol")
        assert _avoids(conn, _UID, "retinol")

        update_decision = CrudDecision(
            op=CrudOp.UPDATE,
            fact=new_fact,
            slot_key="ingredient:레티놀",
            target_memory_id=1,
            existing=existing,
        )
        bridge.project_to_graph(conn, _UID, update_decision, ingredient_key="retinol")
        assert not _avoids(conn, _UID, "retinol")
        assert _prefers(conn, _UID, "retinol")


def test_project_to_graph_delete_removes_edge(conn: psycopg.Connection[object]) -> None:
    _require_seed(conn)
    existing = ActiveMemory(
        memory_id=1,
        fact_type=FactType.AVOID_INGREDIENT,
        slot_key="ingredient:레티놀",
        target_name="레티놀",
        content="레티놀 회피",
        season=None,
    )
    with db.user_scope(conn, _UID):
        add_decision = CrudDecision(
            op=CrudOp.ADD,
            fact=ExtractedFact(
                fact_type=FactType.AVOID_INGREDIENT, content="레티놀 회피", target_name="레티놀"
            ),
            slot_key="ingredient:레티놀",
        )
        bridge.project_to_graph(conn, _UID, add_decision, ingredient_key="retinol")
        assert _avoids(conn, _UID, "retinol")

        delete_decision = CrudDecision(
            op=CrudOp.DELETE,
            fact=ExtractedFact(
                fact_type=FactType.AVOID_INGREDIENT,
                content="철회",
                target_name="레티놀",
                retract=True,
            ),
            slot_key="ingredient:레티놀",
            target_memory_id=1,
            existing=existing,
        )
        bridge.project_to_graph(conn, _UID, delete_decision, ingredient_key="retinol")
        assert not _avoids(conn, _UID, "retinol")


def test_project_to_graph_unresolved_key_skips_silently(conn: psycopg.Connection[object]) -> None:
    """ingredient_key=None(해석 실패) → 예외 없이 그냥 skip(PRD F1 예외)."""
    fact = ExtractedFact(
        fact_type=FactType.AVOID_INGREDIENT, content="회피", target_name="존재하지않는성분XYZ"
    )
    decision = CrudDecision(op=CrudOp.ADD, fact=fact, slot_key="ingredient:존재하지않는성분xyz")
    with db.user_scope(conn, _UID):
        bridge.project_to_graph(conn, _UID, decision, ingredient_key=None)  # 예외 없이 통과


def test_project_to_graph_noop_does_nothing(conn: psycopg.Connection[object]) -> None:
    fact = ExtractedFact(
        fact_type=FactType.AVOID_INGREDIENT, content="레티놀 회피", target_name="레티놀"
    )
    decision = CrudDecision(
        op=CrudOp.NOOP, fact=fact, slot_key="ingredient:레티놀", target_memory_id=1
    )
    with db.user_scope(conn, _UID):
        bridge.project_to_graph(conn, _UID, decision, ingredient_key="retinol")
        assert not _avoids(
            conn, _UID, "retinol"
        )  # no-op 은 엣지도 안 건드림(이미 있었을 엣지 유지 전제)
