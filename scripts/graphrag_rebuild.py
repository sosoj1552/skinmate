"""GraphRAG AGE 그래프 클린 재빌드 스크립트 (docs/graphrag-design.md 검증용).

실패한 덤프 복원이 옛 표현(canonical_key 기반) 그래프 데이터를 남겨서, 지금 그래프에는
TREATS 엣지 254개 등 슬라이스가 넣지 않은 잔재가 섞여 있다. 이 스크립트는 그래프
전체(정점·엣지)를 비우고 7고민 기준 Concern 노드만 심어, 이후 graphrag_slice.py ·
graphrag_extract_slice.py 를 깨끗한 상태에서 재실행해 검증할 수 있게 한다.

그래프 접근은 choke.age_exec 단일 관문만 경유한다. 재실행해도 안전(전체 삭제 후
MERGE 기반 재시드이므로 멱등).

실행: POSTGRES_PASSWORD=change-me PYTHONIOENCODING=utf-8 \
      .venv/Scripts/python.exe scripts/graphrag_rebuild.py
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
import structlog

from skinmate.graph import choke

logger = structlog.get_logger()

# 7고민 기준 시드 노드 (name, label)
CONCERN_SEEDS: list[tuple[str, str]] = [
    ("acne", "트러블"),
    ("dryness", "건조"),
    ("sensitivity", "민감"),
    ("wrinkles", "주름"),
    ("oiliness", "피지"),
    ("dullness", "칙칙함"),
    ("pores", "모공"),
]

# 전체 DETACH DELETE가 실패할 때(큰 그래프) 배치로 나눠 반복 삭제할 단위
BATCH_DELETE_LIMIT = 1000

EDGE_TYPE_COUNT_QUERY = (
    "MATCH ()-[r]->() WITH type(r) AS rel_type, count(r) AS cnt "
    "RETURN {rel_type: rel_type, cnt: cnt}"
)


def _db_url() -> str:
    user = os.getenv("POSTGRES_USER", "skinmate")
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        logger.error("POSTGRES_PASSWORD is not set")
        raise SystemExit(1)
    db_name = os.getenv("POSTGRES_DB", "skinmate")
    port = os.getenv("POSTGRES_PORT", "5432")
    host = os.getenv("POSTGRES_HOST", "localhost")  # 기본값 localhost, Docker 컨테이너 내에서는 db
    return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"


def _scalar(value: Any) -> Any:
    """choke.age_exec 반환값에서 스칼라를 꺼낸다({"value": x} 래핑 형태 대응)."""
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def vertex_count(conn: psycopg.Connection[Any]) -> int:
    rows = choke.age_exec(conn, None, "MATCH (n) RETURN count(n)")
    if not rows:
        return 0
    return int(_scalar(rows[0]))


def report_state(conn: psycopg.Connection[Any], label: str) -> None:
    """정점 총수 + 엣지 타입별 개수를 출력한다(before/after 공용)."""
    vcount = vertex_count(conn)
    print(f"[{label}] 정점 총수: {vcount}")

    edge_rows = choke.age_exec(conn, None, EDGE_TYPE_COUNT_QUERY)
    if not edge_rows:
        print(f"[{label}] 엣지: 0건")
        return

    print(f"[{label}] 엣지 타입별 개수:")
    for row in sorted(edge_rows, key=lambda r: str(r.get("rel_type"))):
        print(f"  {row.get('rel_type')}: {row.get('cnt')}건")


def clear_graph(conn: psycopg.Connection[Any]) -> None:
    """그래프 전체(정점+엣지)를 비운다.

    한 번에 DETACH DELETE 하는 것을 우선 시도하고, 실패하면(큰 그래프에서 타임아웃 등)
    LIMIT으로 나눠 정점이 0개가 될 때까지 반복 삭제한다.
    """
    try:
        choke.age_exec(conn, None, "MATCH (n) DETACH DELETE n")
        conn.commit()
        logger.info("graph_cleared_single_pass")
        return
    except Exception as exc:  # 배치 폴백을 위해 광범위 예외 포착
        conn.rollback()
        logger.warning("single_pass_delete_failed_falling_back_to_batch", error=str(exc))

    while True:
        choke.age_exec(conn, None, f"MATCH (n) WITH n LIMIT {BATCH_DELETE_LIMIT} DETACH DELETE n")
        conn.commit()
        remaining = vertex_count(conn)
        logger.info("batch_delete_progress", remaining=remaining)
        if remaining == 0:
            break


def seed_concerns(conn: psycopg.Connection[Any]) -> None:
    """7고민 기준 Concern 노드를 멱등 MERGE 로 심는다."""
    for name, label in CONCERN_SEEDS:
        choke.age_exec(
            conn,
            None,
            "MERGE (c:Concern {name: $name}) SET c.label = $label",
            {"name": name, "label": label},
        )
    conn.commit()
    logger.info("concern_seeds_merged", count=len(CONCERN_SEEDS))


def main() -> None:
    db_url = _db_url()
    logger.info("graphrag_rebuild_started")

    with psycopg.connect(db_url) as conn:
        print("=== BEFORE (클린 재빌드 전) ===")
        report_state(conn, "before")

        clear_graph(conn)
        seed_concerns(conn)

        print("\n=== AFTER (클린 재빌드 후: 7 Concern만 존재해야 함) ===")
        report_state(conn, "after")


if __name__ == "__main__":
    main()
