"""기억 순위화 — rank_memory(⭐6). A 의 검색 융합(1A.7)이 소비하는 개인 사실 랭킹.

`effective_weight = base_weight × exp(-λ × Δdays)`, **λ=0.05/day**(≈14일 반감기)를
조회 시점에 계산한다(저장 안 함, docs/DATA-MODEL §1). 자주·최근 언급된 사실이 상위로 온다(AC-M2).
반드시 `db.user_scope` 안에서 호출한다(RLS 격리, AC-M5).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from skinmate.contracts.facts import FactType, RankedFact

LAMBDA_PER_DAY = 0.05
"""시간감쇠 상수(1/day). 팀 합의 고정값 — 변경 시 AC-M2 fixture 재보정 필요."""


def effective_weight(base_weight: float, last_seen: datetime, now: datetime) -> float:
    """시간감쇠 반영 가중치. Δdays 는 음수(미래 last_seen, 시계 오차)를 0 으로 클램프."""
    delta_days = max(0.0, (now - last_seen).total_seconds() / 86400.0)
    return base_weight * math.exp(-LAMBDA_PER_DAY * delta_days)


def rank_memory(
    conn: psycopg.Connection[Any],
    user_id: int,
    *,
    now: datetime | None = None,
) -> list[RankedFact]:
    """user 의 활성 기억을 effective_weight 내림차순으로 반환(⭐6, retrieve 입력).

    RLS 로 본인 행만 조회되며(스코프 필수), 방어적으로 user_id 도 필터한다.
    `now` 주입 가능(결정적 테스트). 동률은 memory_id 오름차순으로 안정 정렬.
    """
    now = now or datetime.now(UTC)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT memory_id, fact_type, content, base_weight, frequency,
                   last_seen, season, target_ingredient_id, target_name
            FROM memories
            WHERE user_id = %s AND deleted_at IS NULL
            """,
            (user_id,),
        )
        rows = cur.fetchall()

    facts = [
        RankedFact(
            memory_id=r["memory_id"],
            fact_type=FactType(r["fact_type"]),
            content=r["content"],
            effective_weight=effective_weight(r["base_weight"], r["last_seen"], now),
            frequency=r["frequency"],
            last_seen=r["last_seen"],
            target_ingredient_id=r["target_ingredient_id"],
            target_name=r["target_name"],
            season=r["season"],
        )
        for r in rows
    ]
    facts.sort(key=lambda f: (-f.effective_weight, f.memory_id))
    return facts
