"""그래프 개념 기반 메타패스(GraphRAG W1/W2) 순회 엔진 (WBS 1A.6 → GraphRAG 슬라이스 승격).

질문에서 인식한 피부 고민(concern)을 기점으로 W1(성분→작동원리→고민)·W2(성분→고민 직접)
메타패스를 전역(user_scope=None) 순회하여 추천 성분과 근거 문서(source_doc_ids)를 수집한다.
scripts/graphrag_slice.py·graphrag_extract_slice.py 로 검증한 W1 순회 쿼리·고민 키워드 인식
규칙을 프로덕션으로 승격한 것이다(스크립트 import 없음, choke.age_exec 단일 관문만 경유,
라벨 생성 DDL만 예외적으로 raw SQL — graphrag_slice.py 와 동일 패턴).
"""

from __future__ import annotations

from typing import Any

import psycopg

from skinmate.contracts.facts import FactType
from skinmate.contracts.graph import EdgeRel, GraphEdge, GraphNode, GraphPath, NodeKind
from skinmate.graph import choke
from skinmate.graph.knowledge_populate import CONCERN_RULES
from skinmate.memory.rank import rank_memory

# 질의 속 고민 인식 키워드 규칙(순서=우선순위, 첫 매치).
# scripts/graphrag_extract_slice.py CONCERN_KEYWORD_RULES 를 프로덕션으로 이식한 사본이다
# (프로덕션이 scripts 에 의존하면 안 되므로 별도 유지 — knowledge_populate.py CONCERN_RULES 의
# 키워드는 ingredient intro 텍스트용이라 목적이 달라 재사용하지 않는다).
CONCERN_KEYWORD_RULES: list[tuple[str, list[str]]] = [
    ("acne", ["여드름", "트러블", "여드름성", "뾰루지"]),
    ("dryness", ["건조", "건성"]),
    ("sensitivity", ["민감", "자극"]),
    ("wrinkles", ["주름", "탄력", "노화"]),
    ("oiliness", ["피지", "지성", "유분"]),
    ("dullness", ["칙칙", "미백", "색소", "피부톤"]),
    ("pores", ["모공"]),
]

_KNOWN_CONCERN_CODES = {code for code, _ in CONCERN_KEYWORD_RULES}

# HAS_CONCERN 기억의 target_name 은 한글 라벨("건조")로 저장되는 경우가 흔하다(memory/bridge
# 호출부 관례). knowledge_populate.py CONCERN_RULES 를 단일 출처로 재사용해 라벨→코드 역방향
# 매핑을 만든다(라벨 문자열을 이 파일에서 다시 정의하지 않음).
_CONCERN_LABEL_TO_CODE = {rule["label"]: code for code, rule in CONCERN_RULES.items()}

# W1: 고민 ← [TREATS] ← 작동원리 ← [ACHIEVES] ← 성분
W1_QUERY = """
MATCH (c:Concern {name: $concern_name})<-[t:TREATS]-(m:Mechanism)<-[a:ACHIEVES]-(i:Ingredient)
RETURN {
    concern_label: c.label,
    mech_name: m.name,
    ing_id: i.ingredient_id, ing_name: i.name_ko,
    treats_docs: t.source_doc_ids, achieves_docs: a.source_doc_ids
}
"""

# W2: 고민 ← [TREATS] ← 성분 (작동원리 경유 없이 직접)
W2_QUERY = """
MATCH (c:Concern {name: $concern_name})<-[t:TREATS]-(i:Ingredient)
RETURN {
    concern_label: c.label,
    ing_id: i.ingredient_id, ing_name: i.name_ko,
    treats_docs: t.source_doc_ids
}
"""


def recognize_concern(query: str) -> str | None:
    """질의 문자열에서 피부 고민 코드를 키워드 매칭으로 인식한다(첫 매치 우선순위)."""
    for code, keywords in CONCERN_KEYWORD_RULES:
        if any(kw in query for kw in keywords):
            return code
    return None


def _recognize_concern_from_memory(conn: psycopg.Connection[Any], user_id: int) -> str | None:
    """질의에서 고민을 못 찾았을 때 사용자의 HAS_CONCERN 기억으로 폴백 인식한다."""
    try:
        facts = rank_memory(conn, user_id)
    except psycopg.Error:
        return None
    for fact in facts:
        if fact.fact_type != FactType.HAS_CONCERN or not fact.target_name:
            continue
        target = fact.target_name.strip()
        lowered = target.lower()
        if lowered in _KNOWN_CONCERN_CODES:
            return lowered
        code = _CONCERN_LABEL_TO_CODE.get(target)
        if code is not None:
            return code
    return None


def _ensure_graphrag_labels(conn: psycopg.Connection[Any]) -> None:
    """Mechanism vlabel · ACHIEVES/ENABLES elabel 이 없으면 생성한다(멱등).

    db/migrations/003_graph_ontology.sql 에는 아직 이 라벨들이 없으므로, GraphRAG 순회 전에
    방어적으로 보장한다(scripts/graphrag_slice.py ensure_labels() 와 동일 패턴 — 라벨 생성
    DDL만 choke.age_exec 단일 관문의 예외로 raw SQL을 쓴다).
    """
    with conn.cursor() as cur:
        cur.execute("SET LOCAL search_path = ag_catalog, public;")
        cur.execute("""
            DO $$
            DECLARE
                gid oid;
            BEGIN
                SELECT graphid INTO gid FROM ag_catalog.ag_graph WHERE name = 'skinmate';
                IF gid IS NULL THEN
                    RETURN;
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM ag_catalog.ag_label WHERE name = 'Mechanism' AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_vlabel('skinmate', 'Mechanism');
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM ag_catalog.ag_label WHERE name = 'ACHIEVES' AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_elabel('skinmate', 'ACHIEVES');
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM ag_catalog.ag_label WHERE name = 'ENABLES' AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_elabel('skinmate', 'ENABLES');
                END IF;
            END $$;
            """)


def _int_list(value: Any) -> list[int]:
    if not value:
        return []
    return [int(v) for v in value]


def _w1_paths(conn: psycopg.Connection[Any], concern_code: str) -> list[GraphPath]:
    rows = choke.age_exec(conn, None, W1_QUERY, {"concern_name": concern_code})
    paths: list[GraphPath] = []
    for row in rows:
        val = row.get("value", row) if isinstance(row, dict) else row
        if not isinstance(val, dict) or val.get("ing_id") is None or val.get("mech_name") is None:
            continue
        mech_name = str(val["mech_name"])
        nodes = [
            GraphNode(kind=NodeKind.CONCERN, key=concern_code, label=val.get("concern_label")),
            GraphNode(kind=NodeKind.MECHANISM, key=mech_name, label=mech_name),
            GraphNode(
                kind=NodeKind.INGREDIENT, key=str(int(val["ing_id"])), label=val.get("ing_name")
            ),
        ]
        edges = [
            GraphEdge(
                rel=EdgeRel.ACHIEVES,
                from_idx=2,
                to_idx=1,
                source_doc_ids=_int_list(val.get("achieves_docs")),
            ),
            GraphEdge(
                rel=EdgeRel.TREATS,
                from_idx=1,
                to_idx=0,
                source_doc_ids=_int_list(val.get("treats_docs")),
            ),
        ]
        try:
            paths.append(GraphPath(nodes=nodes, edges=edges))
        except ValueError:
            continue
    return paths


def _w2_paths(conn: psycopg.Connection[Any], concern_code: str) -> list[GraphPath]:
    rows = choke.age_exec(conn, None, W2_QUERY, {"concern_name": concern_code})
    paths: list[GraphPath] = []
    for row in rows:
        val = row.get("value", row) if isinstance(row, dict) else row
        if not isinstance(val, dict) or val.get("ing_id") is None:
            continue
        nodes = [
            GraphNode(kind=NodeKind.CONCERN, key=concern_code, label=val.get("concern_label")),
            GraphNode(
                kind=NodeKind.INGREDIENT, key=str(int(val["ing_id"])), label=val.get("ing_name")
            ),
        ]
        edges = [
            GraphEdge(
                rel=EdgeRel.TREATS,
                from_idx=1,
                to_idx=0,
                source_doc_ids=_int_list(val.get("treats_docs")),
            ),
        ]
        try:
            paths.append(GraphPath(nodes=nodes, edges=edges))
        except ValueError:
            continue
    return paths


def traverse_recommendation_paths(
    conn: psycopg.Connection[Any],
    user_id: int,
    query: str,
    season: str | None = None,
) -> list[GraphPath]:
    """질의에서 인식한 피부 고민을 기점으로 W1/W2 개념 기반 메타패스를 순회한다.

    W1: (:Concern)<-[:TREATS]-(:Mechanism)<-[:ACHIEVES]-(:Ingredient) — 성분→작동원리→고민.
    W2: (:Concern)<-[:TREATS]-(:Ingredient) — 성분→고민 직접.
    둘 다 전역 지식 순회이므로 user_scope=None(choke.age_exec). 질의에서 고민을 인식하지
    못하면 사용자의 HAS_CONCERN 기억으로 폴백하고, 그마저 없으면 빈 리스트를 반환한다.

    (참고) read-through 캐시(traverse_cache)는 이번 개편에서 비활성화한다 — 기존 캐시 키가
    (user_id, season) 뿐이라 질의마다 달라지는 고민 인식 결과를 반영할 수 없다(추후 캐시
    키에 concern_code 를 포함해 재도입 가능). season 인자는 호출부 하위호환을 위해 시그니처만
    유지하며 현재 W1/W2 메타패스는 계절을 쓰지 않는다.
    """
    del season  # 현재 개념 기반 메타패스는 계절 미사용(시그니처 하위호환만 유지)

    concern_code = recognize_concern(query)
    if concern_code is None:
        concern_code = _recognize_concern_from_memory(conn, user_id)
    if concern_code is None:
        return []

    _ensure_graphrag_labels(conn)

    return _w1_paths(conn, concern_code) + _w2_paths(conn, concern_code)


def generate_rationale_from_path(path: GraphPath) -> str:
    """GraphPath 를 해석해 사람이 읽는 추천 근거 문장을 생성한다.

    개념 기반 메타패스(W1: 고민←작동원리←성분, W2: 고민←성분)만 방출되는 현재 순회 결과에
    맞춰 "성분→작동원리→고민" 형태의 이유를 자연어로 표현한다.
    """
    if path.nodes[0].kind == NodeKind.CONCERN:
        # W1: [Concern, Mechanism, Ingredient]
        if len(path.nodes) == 3 and path.nodes[1].kind == NodeKind.MECHANISM:
            c, m, i = path.nodes
            return (
                f"{i.label or i.key} 성분이 {m.label or m.key} 작용을 통해 "
                f"{c.label or c.key} 고민 완화에 도움을 줍니다."
            )
        # W2: [Concern, Ingredient]
        if len(path.nodes) == 2 and path.nodes[1].kind == NodeKind.INGREDIENT:
            c, i = path.nodes
            return f"{i.label or i.key} 성분이 {c.label or c.key} 고민 완화에 도움을 줍니다."

    return "추천 경로에 알맞은 매칭 성분이 발견되었습니다."
