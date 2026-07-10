"""그래프 2+hop 순회 및 추천 근거 생성 엔진 (WBS 1A.6).

사용자 격리(GUC user_scope) 하에 choke.age_exec를 경유하여
고민, 선호/기피 성분 경로를 순회하고 GraphPath Pydantic 모델로 가공하여 방출합니다.
"""

from __future__ import annotations

import json
from typing import Any

import psycopg

from skinmate.contracts.graph import EdgeRel, GraphEdge, GraphNode, GraphPath, NodeKind
from skinmate.graph import choke


def traverse_recommendation_paths(
    conn: psycopg.Connection[Any],
    user_id: int,
    season: str | None = None,
) -> list[GraphPath]:
    """사용자 격리 영역 내에서 2+hop 그래프 경로를 순회 탐색합니다. (Read-Through 캐시 적용)

    사용자 기피/선호 성분 매핑, 피부 고민 완화 성분, 대안 성분 우회 경로를 수집하여
    Pydantic GraphPath 객체의 리스트로 반환합니다.
    """
    season_key = season if season is not None else ""

    # 1. 캐시 조회 시도
    with conn.cursor() as cur:
        cur.execute(
            "SELECT paths_json FROM public.traverse_cache WHERE user_id = %s AND season = %s;",
            (user_id, season_key),
        )
        row = cur.fetchone()
        if row is not None:
            paths_data = row[0]
            if isinstance(paths_data, str):
                paths_data = json.loads(paths_data)
            return [GraphPath.model_validate(p) for p in paths_data]

    # 2. 캐시 미스 시 Cypher 쿼리 수행
    paths: list[GraphPath] = []

    # 1. Avoidance Paths (기피 성분 함유 제품 경로)
    # (:User)-[:AVOIDS]->(:Ingredient)<-[:CONTAINS]-(:Product)
    avoid_query = """
    MATCH (u:User {user_id: $user_scope})-[r1:AVOIDS]->(i:Ingredient)<-[r2:CONTAINS]-(p:Product)
    RETURN {
        u_id: u.user_id,
        r1_rel: type(r1),
        i_key: i.canonical_key, i_name: i.name,
        r2_rel: type(r2),
        p_id: p.product_id, p_name: p.name, p_brand: p.brand
    }
    """
    avoid_rows = choke.age_exec(conn, user_id, avoid_query)
    for row in avoid_rows:
        try:
            # age_exec 반환 데이터 래핑 처리
            val = row.get("value", row) if isinstance(row, dict) else row
            if not isinstance(val, dict) or "i_key" not in val or "p_id" not in val:
                continue

            nodes = [
                GraphNode(kind=NodeKind.USER, key=str(val["u_id"]), label="사용자"),
                GraphNode(kind=NodeKind.INGREDIENT, key=val["i_key"], label=val["i_name"]),
                GraphNode(kind=NodeKind.PRODUCT, key=str(val["p_id"]), label=val["p_name"]),
            ]
            edges = [
                GraphEdge(rel=EdgeRel.AVOIDS, from_idx=0, to_idx=1),
                GraphEdge(rel=EdgeRel.CONTAINS, from_idx=2, to_idx=1),
            ]
            paths.append(GraphPath(nodes=nodes, edges=edges))
        except (KeyError, ValueError):
            continue

    # 2. Preference Paths (선호 성분 함유 제품 경로)
    # (:User)-[:PREFERS]->(:Ingredient)<-[:CONTAINS]-(:Product)
    prefer_query = """
    MATCH (u:User {user_id: $user_scope})-[r1:PREFERS]->(i:Ingredient)<-[r2:CONTAINS]-(p:Product)
    RETURN {
        u_id: u.user_id,
        r1_rel: type(r1),
        i_key: i.canonical_key, i_name: i.name,
        r2_rel: type(r2),
        p_id: p.product_id, p_name: p.name, p_brand: p.brand
    }
    """
    prefer_rows = choke.age_exec(conn, user_id, prefer_query)
    for row in prefer_rows:
        try:
            val = row.get("value", row) if isinstance(row, dict) else row
            if not isinstance(val, dict) or "i_key" not in val or "p_id" not in val:
                continue

            nodes = [
                GraphNode(kind=NodeKind.USER, key=str(val["u_id"]), label="사용자"),
                GraphNode(kind=NodeKind.INGREDIENT, key=val["i_key"], label=val["i_name"]),
                GraphNode(kind=NodeKind.PRODUCT, key=str(val["p_id"]), label=val["p_name"]),
            ]
            edges = [
                GraphEdge(rel=EdgeRel.PREFERS, from_idx=0, to_idx=1),
                GraphEdge(rel=EdgeRel.CONTAINS, from_idx=2, to_idx=1),
            ]
            paths.append(GraphPath(nodes=nodes, edges=edges))
        except (KeyError, ValueError):
            continue

    # 3. Treatment Paths (피부 고민 해결 경로)
    # (:User)-[:HAS_CONCERN]->(:Concern)<-[:TREATS]-(:Ingredient)<-[:CONTAINS]-(:Product)
    treat_query = """
    MATCH (u:User {user_id: $user_scope})-[r1:HAS_CONCERN]->(c:Concern)
    MATCH (c)<-[r2:TREATS]-(i:Ingredient)<-[r3:CONTAINS]-(p:Product)
    RETURN {
        u_id: u.user_id,
        r1_rel: type(r1), r1_season: r1.season,
        c_name: c.name, c_label: c.label,
        r2_rel: type(r2),
        i_key: i.canonical_key, i_name: i.name,
        r3_rel: type(r3),
        p_id: p.product_id, p_name: p.name, p_brand: p.brand
    }
    """
    treat_rows = choke.age_exec(conn, user_id, treat_query)
    for row in treat_rows:
        try:
            val = row.get("value", row) if isinstance(row, dict) else row
            if not isinstance(val, dict) or "c_name" not in val or "i_key" not in val:
                continue

            # 계절 필터링 매칭 (season 파라미터가 제공된 경우 일치 여부 확인)
            r1_season = val.get("r1_season")
            if season is not None and r1_season is not None and r1_season != season:
                continue

            nodes = [
                GraphNode(kind=NodeKind.USER, key=str(val["u_id"]), label="사용자"),
                GraphNode(kind=NodeKind.CONCERN, key=val["c_name"], label=val["c_label"]),
                GraphNode(kind=NodeKind.INGREDIENT, key=val["i_key"], label=val["i_name"]),
                GraphNode(kind=NodeKind.PRODUCT, key=str(val["p_id"]), label=val["p_name"]),
            ]
            edges = [
                GraphEdge(rel=EdgeRel.HAS_CONCERN, from_idx=0, to_idx=1, season=r1_season),
                GraphEdge(rel=EdgeRel.TREATS, from_idx=2, to_idx=1),
                GraphEdge(rel=EdgeRel.CONTAINS, from_idx=3, to_idx=2),
            ]
            paths.append(GraphPath(nodes=nodes, edges=edges))
        except (KeyError, ValueError):
            continue

    # 4. Alternative Paths (대안 성분 우회 추천 경로)
    # (:User)-[:AVOIDS]->(i1:Ingredient)-[:TREATS]->(:Concern)
    #               <-[:TREATS]-(i2:Ingredient)<-[:CONTAINS]-(:Product)
    alt_query = """
    MATCH (u:User {user_id: $user_scope})-[r1:AVOIDS]->(i1:Ingredient)
    MATCH (i1)-[r2:TREATS]->(c:Concern)
    MATCH (c)<-[r3:TREATS]-(i2:Ingredient)<-[r4:CONTAINS]-(p:Product)
    WHERE i1.canonical_key <> i2.canonical_key
    RETURN {
        u_id: u.user_id,
        r1_rel: type(r1),
        i1_key: i1.canonical_key, i1_name: i1.name,
        r2_rel: type(r2),
        c_name: c.name, c_label: c.label,
        r3_rel: type(r3),
        i2_key: i2.canonical_key, i2_name: i2.name,
        r4_rel: type(r4),
        p_id: p.product_id, p_name: p.name, p_brand: p.brand
    }
    """
    alt_rows = choke.age_exec(conn, user_id, alt_query)
    for row in alt_rows:
        try:
            val = row.get("value", row) if isinstance(row, dict) else row
            if not isinstance(val, dict) or "i1_key" not in val or "i2_key" not in val:
                continue

            nodes = [
                GraphNode(kind=NodeKind.USER, key=str(val["u_id"]), label="사용자"),
                GraphNode(kind=NodeKind.INGREDIENT, key=val["i1_key"], label=val["i1_name"]),
                GraphNode(kind=NodeKind.CONCERN, key=val["c_name"], label=val["c_label"]),
                GraphNode(kind=NodeKind.INGREDIENT, key=val["i2_key"], label=val["i2_name"]),
                GraphNode(kind=NodeKind.PRODUCT, key=str(val["p_id"]), label=val["p_name"]),
            ]
            edges = [
                GraphEdge(rel=EdgeRel.AVOIDS, from_idx=0, to_idx=1),
                GraphEdge(rel=EdgeRel.TREATS, from_idx=1, to_idx=2),
                GraphEdge(rel=EdgeRel.TREATS, from_idx=3, to_idx=2),
                GraphEdge(rel=EdgeRel.CONTAINS, from_idx=4, to_idx=3),
            ]
            paths.append(GraphPath(nodes=nodes, edges=edges))
        except (KeyError, ValueError):
            continue

    # 3. 계산된 결과 캐시에 저장
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO public.traverse_cache (user_id, season, paths_json)
            VALUES (%s, %s, %s)
            ON CONFLICT (user_id, season)
            DO UPDATE SET paths_json = EXCLUDED.paths_json, created_at = CURRENT_TIMESTAMP;
            """,
            (user_id, season_key, json.dumps([p.model_dump() for p in paths])),
        )

    return paths


def generate_rationale_from_path(path: GraphPath) -> str:
    """GraphPath의 노드와 관계를 해석하여 사용자 맞춤형 추천 근거 문장을 생성합니다."""
    # 엣지 맵 매핑 구조화
    edge_types = {e.rel for e in path.edges}

    # 1. Alternative Path 케이스
    # AVOIDS 엣지와 TREATS(대안) 엣지가 함께 공존
    if EdgeRel.AVOIDS in edge_types and len(path.nodes) == 5:
        # nodes: [User, i1(Avoided), Concern, i2(Alt), Product]
        u, i1, c, i2, p = path.nodes
        return (
            f"기피하시는 {i1.label or i1.key} 성분을 피하면서, "
            f"동일하게 피부 {c.label or c.key} 고민을 해결해주는 "
            f"대체 성분 {i2.label or i2.key}이(가) 함유된 {p.label or p.key} 제품을 추천합니다."
        )

    # 2. Avoidance Path 케이스
    # AVOIDS 엣지만 존재
    if EdgeRel.AVOIDS in edge_types and len(path.nodes) == 3:
        # nodes: [User, Ingredient, Product]
        u, i, p = path.nodes
        return f"기피 성분인 {i.label or i.key} 성분이 {p.label or p.key} 제품에 함유되어 있습니다."

    # 3. Preference Path 케이스
    # PREFERS 엣지 존재
    if EdgeRel.PREFERS in edge_types and len(path.nodes) == 3:
        # nodes: [User, Ingredient, Product]
        u, i, p = path.nodes
        return f"선호하시는 {i.label or i.key} 성분이 함유된 {p.label or p.key} 제품을 추천합니다."

    # 4. Treatment Path 케이스
    # HAS_CONCERN 엣지 존재
    if EdgeRel.HAS_CONCERN in edge_types and len(path.nodes) == 4:
        # nodes: [User, Concern, Ingredient, Product]
        u, c, i, p = path.nodes
        # 계절 정보 탐색
        has_concern_edge = next((e for e in path.edges if e.rel == EdgeRel.HAS_CONCERN), None)
        season = has_concern_edge.season if has_concern_edge else None

        if season:
            return (
                f"{season}철 고민인 {c.label or c.key}을(를) 완화해주는 "
                f"{i.label or i.key} 성분이 함유된 {p.label or p.key} 제품을 추천합니다."
            )
        return (
            f"피부 고민인 {c.label or c.key}을(를) 완화해주는 "
            f"{i.label or i.key} 성분이 함유된 {p.label or p.key} 제품을 추천합니다."
        )

    return "추천 경로에 알맞은 매칭 성분이 발견되었습니다."
