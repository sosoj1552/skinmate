"""CRUD 판정기(1B.2) — 추출된 사실을 기존 기억과 대조해 add/update/delete/no-op 결정(AC-M1).

판정은 slot_key 기반 **결정적 규칙**이다(스키마 memories.slot_key 가 이 용도). 철회(retract) 발화만
delete 로 간다(하드삭제 금지 → soft-delete+감사, PRD F1). `judge` 는 순수 함수(테스트 결정적),
`apply_decision` 은 판정을 memories/memory_audit 에 반영한다 — 트랜잭션·advisory lock 은
writer(1B.4)가 열고, 여기선 커밋하지 않는다. 반드시 db.user_scope(RLS) 안에서 호출한다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import psycopg
from pydantic import BaseModel

from skinmate.contracts.facts import FactType
from skinmate.memory import repo
from skinmate.memory.extract import ExtractedFact
from skinmate.memory.repo import ActiveMemory

_INGREDIENT_TYPES = {FactType.AVOID_INGREDIENT, FactType.PREFER_INGREDIENT}
_BRAND_TYPES = {FactType.AVOID_BRAND, FactType.PREFER_BRAND}
_AVOID_TYPES = {FactType.AVOID_INGREDIENT, FactType.AVOID_BRAND}


class CrudOp(StrEnum):
    """memory_audit.op 과 값 일치(add/update/delete/no-op)."""

    ADD = "add"
    UPDATE = "update"
    DELETE = "delete"
    NOOP = "no-op"


class CrudDecision(BaseModel):
    """판정 결과. target/existing 은 update/delete/no-op 에서 채워진다."""

    op: CrudOp
    fact: ExtractedFact
    slot_key: str | None = None
    target_memory_id: int | None = None
    existing: ActiveMemory | None = None


def _norm(name: str | None) -> str:
    return (name or "").strip().lower()


def slot_key_for(fact: ExtractedFact) -> str | None:
    """사실의 슬롯 키 — 같은 슬롯 = update/no-op 판정 단위. 무명/other 는 None(항상 add).

    성분·브랜드는 회피/선호가 **같은 슬롯**(입장 전환=update). skin_type 은 단일 슬롯.
    """
    if fact.fact_type == FactType.SKIN_TYPE:
        return "skin_type"
    subject = _norm(fact.target_name)
    if not subject:
        return None
    if fact.fact_type in _INGREDIENT_TYPES:
        return f"ingredient:{subject}"
    if fact.fact_type in _BRAND_TYPES:
        return f"brand:{subject}"
    if fact.fact_type == FactType.HAS_CONCERN:
        return f"concern:{subject}"
    return None


def _slot_value(fact_type: FactType, target_name: str | None, content: str) -> str:
    """슬롯 안의 '값'. 같으면 중복 언급(no-op), 다르면 전환(update)."""
    if fact_type in _INGREDIENT_TYPES or fact_type in _BRAND_TYPES:
        return "avoid" if fact_type in _AVOID_TYPES else "prefer"
    if fact_type == FactType.SKIN_TYPE:
        return _norm(target_name) or _norm(content)
    if fact_type == FactType.HAS_CONCERN:
        return "present"
    return _norm(content)


def judge(existing: list[ActiveMemory], fact: ExtractedFact) -> CrudDecision:
    """추출 사실 1건 → CRUD 판정(순수). existing 은 같은 user 의 활성 기억 목록."""
    slot = slot_key_for(fact)
    match = next((m for m in existing if slot is not None and m.slot_key == slot), None)

    if fact.retract:
        if match is not None:
            return CrudDecision(
                op=CrudOp.DELETE,
                fact=fact,
                slot_key=slot,
                target_memory_id=match.memory_id,
                existing=match,
            )
        return CrudDecision(op=CrudOp.NOOP, fact=fact, slot_key=slot)  # 철회할 대상 없음

    if slot is None or match is None:  # 신규 슬롯 → 비파괴 add
        return CrudDecision(op=CrudOp.ADD, fact=fact, slot_key=slot)

    same_value = _slot_value(match.fact_type, match.target_name, match.content) == _slot_value(
        fact.fact_type, fact.target_name, fact.content
    )
    op = CrudOp.NOOP if same_value else CrudOp.UPDATE
    return CrudDecision(
        op=op, fact=fact, slot_key=slot, target_memory_id=match.memory_id, existing=match
    )


def _fact_val(fact: ExtractedFact, slot_key: str | None) -> dict[str, Any]:
    return {
        "fact_type": str(fact.fact_type),
        "content": fact.content,
        "target_name": fact.target_name,
        "season": fact.season,
        "slot_key": slot_key,
    }


def _active_val(m: ActiveMemory) -> dict[str, Any]:
    return {
        "fact_type": str(m.fact_type),
        "content": m.content,
        "target_name": m.target_name,
        "season": m.season,
        "slot_key": m.slot_key,
    }


def apply_decision(
    conn: psycopg.Connection[Any],
    user_id: int,
    decision: CrudDecision,
    *,
    target_ingredient_id: int | None = None,
) -> int | None:
    """판정을 memories/memory_audit 에 반영(감사 포함). 반환: 영향받은 memory_id.

    target_ingredient_id 는 writer(1B.4)가 bridge.resolve_ingredient 로 미리 해석해 전달한다
    (성분 사실의 그래프 다리 FK, DATA-MODEL §1). 커밋하지 않는다(트랜잭션은 writer 담당).
    user_scope 안에서 호출해야 RLS 적용.
    """
    fact = decision.fact
    new_val = _fact_val(fact, decision.slot_key)

    if decision.op == CrudOp.ADD:
        mid = repo.insert_memory(
            conn,
            user_id=user_id,
            content=fact.content,
            fact_type=fact.fact_type,
            slot_key=decision.slot_key,
            season=fact.season,
            target_name=fact.target_name,
            target_ingredient_id=target_ingredient_id,
        )
        repo.insert_audit(
            conn, user_id=user_id, memory_id=mid, op="add", old_val=None, new_val=new_val
        )
        return mid

    if decision.op == CrudOp.NOOP:
        if decision.target_memory_id is not None:
            repo.bump_mention(conn, decision.target_memory_id)
        repo.insert_audit(
            conn,
            user_id=user_id,
            memory_id=decision.target_memory_id,
            op="no-op",
            old_val=None,
            new_val=new_val,
        )
        return decision.target_memory_id

    # update/delete 는 대상 행이 있어야 함
    assert decision.target_memory_id is not None
    old_val = _active_val(decision.existing) if decision.existing is not None else None

    if decision.op == CrudOp.UPDATE:
        repo.update_memory(
            conn,
            decision.target_memory_id,
            content=fact.content,
            fact_type=fact.fact_type,
            slot_key=decision.slot_key,
            season=fact.season,
            target_name=fact.target_name,
            target_ingredient_id=target_ingredient_id,
        )
        repo.insert_audit(
            conn,
            user_id=user_id,
            memory_id=decision.target_memory_id,
            op="update",
            old_val=old_val,
            new_val=new_val,
        )
        return decision.target_memory_id

    # DELETE — soft-delete + 감사(하드삭제 금지)
    repo.soft_delete_memory(conn, decision.target_memory_id)
    repo.insert_audit(
        conn,
        user_id=user_id,
        memory_id=decision.target_memory_id,
        op="delete",
        old_val=old_val,
        new_val=None,
    )
    return decision.target_memory_id
