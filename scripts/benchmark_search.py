"""검색(비-LLM) + AGE 2-hop 순회 성능 벤치마크 (WBS 2.6, 성능 예산: 검색 p95 ≤ 400ms).

retrieve_recommendation_context 는 embed_text 가 기본적으로 결정론적 스텁 모드(진짜 LLM/임베딩
모델 호출 없음)이므로, 이 스크립트 전체가 순수 DB(pgvector+AGE) 성능만 측정한다.
traverse_recommendation_paths 는 read-through 캐시(1A.9)가 있어 콜드(캐시 미스)와 웜(캐시 히트)
타이밍을 분리해서 보여준다.

실행: .venv/Scripts/python.exe scripts/benchmark_search.py
"""

from __future__ import annotations

import os
import statistics
import time

import psycopg

from skinmate import db
from skinmate.graph.traverse import traverse_recommendation_paths
from skinmate.retrieval.retrieve import retrieve_recommendation_context

_QUERIES = [
    "가을이라 건조한데 에멀전 추천해줘",
    "트러블 진정에 좋은 성분 뭐가 있을까",
    "주름 개선 세럼 추천",
    "민감성 피부에 순한 제품",
    "속건조 잡아주는 보습제",
]


def _percentile(values: list[float], p: float) -> float:
    values_sorted = sorted(values)
    idx = min(int(len(values_sorted) * p), len(values_sorted) - 1)
    return values_sorted[idx]


def _report(label: str, durations_ms: list[float], budget_ms: float) -> None:
    p50 = _percentile(durations_ms, 0.50)
    p95 = _percentile(durations_ms, 0.95)
    verdict = "OK" if p95 <= budget_ms else "OVER BUDGET"
    print(
        f"[{label}] n={len(durations_ms)} "
        f"mean={statistics.mean(durations_ms):.1f}ms p50={p50:.1f}ms p95={p95:.1f}ms "
        f"(budget {budget_ms:.0f}ms) -> {verdict}"
    )


def main() -> None:
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://skinmate_app:skinmate-app-dev-only@localhost:5432/skinmate",
    )
    conn = psycopg.connect(db_url, autocommit=False)

    # 1. retrieve_recommendation_context 종합 벤치마크(20회, 시드 유저 1001)
    # 실제 process_turn(2.1)과 동일하게 db.user_scope 안에서 호출한다(RLS 스코프 필수).
    retrieve_durations: list[float] = []
    for i in range(20):
        query = _QUERIES[i % len(_QUERIES)]
        start = time.perf_counter()
        with db.user_scope(conn, 1001):
            retrieve_recommendation_context(
                conn, user_id=1001, query=query, season="가을", limit=10
            )
        retrieve_durations.append((time.perf_counter() - start) * 1000)
    _report("retrieve_recommendation_context(비-LLM 검색)", retrieve_durations, 400.0)

    # 2. AGE 2-hop 순회 단독 벤치마크 — 콜드(캐시 무효화 후) vs 웜(캐시 히트)
    with db.user_scope(conn, 1001):
        conn.execute("DELETE FROM public.traverse_cache WHERE user_id = 1001;")

    cold_start = time.perf_counter()
    with db.user_scope(conn, 1001):
        traverse_recommendation_paths(conn, user_id=1001, season="가을")
    cold_ms = (time.perf_counter() - cold_start) * 1000
    print(f"[traverse_recommendation_paths 콜드(캐시 미스)] {cold_ms:.1f}ms")

    warm_durations: list[float] = []
    for _ in range(20):
        start = time.perf_counter()
        with db.user_scope(conn, 1001):
            traverse_recommendation_paths(conn, user_id=1001, season="가을")
        warm_durations.append((time.perf_counter() - start) * 1000)
    _report("traverse_recommendation_paths 웜(캐시 히트)", warm_durations, 400.0)

    conn.close()


if __name__ == "__main__":
    main()
