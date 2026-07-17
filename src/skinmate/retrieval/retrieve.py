"""검색 융합 및 제형 soft-ranking 엔진 (WBS 1A.7).

pgvector 유사 제품 검색 결과에 사용자의 기피 성분을 Hard-filter 처리하고,
사용자 memories 분석을 기반으로 한 제형(에멀전/오일) soft-ranking 가중치를 보정합니다.
최종적으로 RAG 문서 검색 및 2-hop 그래프 경로를 수집하여 RetrievalContext로 산출합니다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from skinmate.config import settings
from skinmate.contracts.facts import FactType
from skinmate.contracts.graph import GraphPath, NodeKind
from skinmate.contracts.refs import ProductRef
from skinmate.contracts.retrieval import DocHit, RetrievalContext
from skinmate.documents.embed import embed_text
from skinmate.documents.search import search_documents
from skinmate.graph.traverse import traverse_recommendation_paths
from skinmate.memory.rank import rank_memory


def _recommended_ingredient_ids(graph_paths: list[GraphPath]) -> list[int]:
    """W1/W2 그래프 경로에서 추천 성분(Ingredient 노드, key=ingredient_id)을 모은다."""
    ids: set[int] = set()
    for path in graph_paths:
        for node in path.nodes:
            if node.kind != NodeKind.INGREDIENT:
                continue
            try:
                ids.add(int(node.key))
            except ValueError:
                continue
    return sorted(ids)


def _graph_source_doc_ids(graph_paths: list[GraphPath]) -> list[int]:
    """그래프 경로 엣지의 source_doc_ids(근거 문서 provenance)를 모은다."""
    ids: set[int] = set()
    for path in graph_paths:
        for edge in path.edges:
            ids.update(edge.source_doc_ids)
    return sorted(ids)


def _fetch_graph_evidence_docs(conn: psycopg.Connection[Any], doc_ids: list[int]) -> list[DocHit]:
    """그래프 경로가 인용한 근거 문서를 회수해 DocHit으로 만든다(그래프 근거 우선)."""
    if not doc_ids:
        return []
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT doc_id, left(content, 300) AS excerpt, source_meta "
            "FROM documents WHERE doc_id = ANY(%s);",
            (doc_ids,),
        )
        rows = cur.fetchall()
    return [
        DocHit(
            doc_id=r["doc_id"],
            content=r["excerpt"],
            score=1.0,
            source_meta=r["source_meta"] if isinstance(r["source_meta"], dict) else {},
        )
        for r in rows
    ]


def retrieve_recommendation_context(
    conn: psycopg.Connection[Any],
    user_id: int,
    query: str,
    season: str | None = None,
    limit: int = 5,
    now: datetime | None = None,
) -> RetrievalContext:
    """RAG 벡터 검색, RLS 스코프 개인 기억, 그래프 2-hop 경로를 하나로 합쳐

    최종 RetrievalContext를 반환합니다.
    사용자의 제형 선호도에 따른 soft-ranking 및 기피 성분의 완전 차단을 보장합니다.
    """
    now = now or datetime.now(UTC)

    # 1. 사용자의 활성 기억 랭킹 조회 및 제형 선호도 식별
    memory_facts = rank_memory(conn, user_id, now=now)

    # 제형 키워드 룰매칭
    prefers_emulsion = False
    avoids_oil = False

    for fact in memory_facts:
        content = fact.content or ""
        # 에멀전 선호 감지
        if ("에멀전" in content or "에멀젼" in content) and not any(
            nw in content for nw in ["싫어", "기피", "피함", "아님"]
        ):
            prefers_emulsion = True
        # 오일 기피 감지
        if "오일" in content and any(nw in content for nw in ["싫어", "기피", "피함", "아님"]):
            avoids_oil = True

    # 기피 성분 ID 수집 (Hard-filter용)
    raw_avoid_ids = [
        fact.target_ingredient_id
        for fact in memory_facts
        if fact.fact_type == FactType.AVOID_INGREDIENT and fact.target_ingredient_id is not None
    ]

    # 1.5 그래프 개념 기반 메타패스(W1/W2) 순회 — 질의에서 인식한 고민에 대한 추천 성분 +
    # 근거 문서(source_doc_ids)를 먼저 수집한다. 하드필터·doc_hits 조립에 모두 쓰인다.
    graph_paths = traverse_recommendation_paths(conn, user_id, query, season=season)
    recommended_ingredient_ids = _recommended_ingredient_ids(graph_paths)
    avoid_ingredient_ids = []
    if raw_avoid_ids:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH target_ingredients AS (
                    SELECT canonical_key, name_ko 
                    FROM ingredients 
                    WHERE ingredient_id = ANY(%s)
                )
                SELECT i.ingredient_id 
                FROM ingredients i
                JOIN target_ingredients t
                  ON i.canonical_key = t.canonical_key
                  OR i.canonical_key = t.name_ko
                  OR t.canonical_key = i.name_ko
                  OR i.name_ko = t.name_ko
                  OR i.name_ko LIKE t.name_ko || '(%%'
                  OR t.name_ko LIKE i.name_ko || '(%%'
                  OR lower(i.canonical_key) = lower(t.canonical_key);
                """,
                (raw_avoid_ids,),
            )
            avoid_ingredient_ids = [row[0] for row in cur.fetchall()]

    # 2. pgvector 기반 제품 코사인 유사도 검색 수행
    query_vector = embed_text(query)

    # 쿼리 조립 및 기피성분 하드필터링 + 그래프 추천성분 필터링
    sql = """
        SELECT
            p.product_id,
            p.name,
            p.brand,
            p.category,
            p.description,
            1.0 - (p.embedding <=> %s::vector) AS similarity
        FROM products p
        WHERE p.embedding IS NOT NULL
    """
    params: list[Any] = [query_vector]

    if avoid_ingredient_ids:
        sql += """
            AND p.product_id NOT IN (
                SELECT DISTINCT pi.product_id
                FROM product_ingredients pi
                WHERE pi.ingredient_id = ANY(%s)
            )
        """
        params.append(avoid_ingredient_ids)

    # SPECIFIC 고민 질의는 그래프 우선: 추천 성분이 있으면 그 성분을 함유한 제품으로 좁힌다.
    # 그래프 경로가 없으면(고민 미인식 등) 기존 pgvector 전체 검색으로 폴백한다.
    if recommended_ingredient_ids:
        sql += """
            AND p.product_id IN (
                SELECT DISTINCT pi.product_id
                FROM product_ingredients pi
                WHERE pi.ingredient_id = ANY(%s)
            )
        """
        params.append(recommended_ingredient_ids)

    # 모든 제품 후보 수집
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        product_rows = cur.fetchall()

    # 3. 제형 Soft-ranking 점수 가중치 보정
    scored_products = []
    for row in product_rows:
        similarity = float(row["similarity"]) if row["similarity"] is not None else 0.0
        desc = row["description"] or ""
        name = row["name"] or ""
        text_context = desc + " " + name

        # 에멀전 선호 가중치 부여
        if prefers_emulsion and ("에멀전" in text_context or "에멀젼" in text_context):
            similarity += 0.15
        # 오일 기피 감정 가중치 삭감
        if avoids_oil and "오일" in text_context:
            similarity -= 0.15

        scored_products.append(
            (
                similarity,
                ProductRef(
                    product_id=row["product_id"],
                    name=row["name"],
                    brand=row["brand"],
                    category=row["category"],
                    # 근거 생성 LLM 이 제형·용도를 실데이터로 판단할 수 있는 만큼만 절단 전달
                    description=(desc[:150] or None),
                ),
            )
        )

    # 소프트 랭킹 점수 내림차순 정렬 후 상위 limit개 선택
    scored_products.sort(key=lambda x: -x[0])
    final_products = [item[1] for item in scored_products[:limit]]

    # 4. RAG 문서 유사도 검색 + 그래프 근거 문서(graph_paths의 source_doc_ids) 병합
    # 그래프 근거를 우선하고, 벡터 검색 결과 중 중복 doc_id 는 제외한다.
    graph_doc_ids = _graph_source_doc_ids(graph_paths)
    graph_doc_hits = _fetch_graph_evidence_docs(conn, graph_doc_ids)
    graph_doc_id_set = {d.doc_id for d in graph_doc_hits}

    # 최종 후보 제품들의 성분(한글명) 목록 일괄 조회 및 바인딩
    if final_products:
        product_ids = [p.product_id for p in final_products]
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pi.product_id, i.name_ko 
                FROM product_ingredients pi
                JOIN ingredients i ON i.ingredient_id = pi.ingredient_id
                WHERE pi.product_id = ANY(%s)
                ORDER BY pi.product_id, i.name_ko;
                """,
                (product_ids,),
            )
            from collections import defaultdict

            ing_map = defaultdict(list)
            for pid, name_ko in cur.fetchall():
                if name_ko:
                    ing_map[pid].append(name_ko)
            for p in final_products:
                p.ingredients = ing_map[p.product_id]

    # 4. RAG 문서 유사도 검색 실행
    doc_results = search_documents(settings.database_url, query, limit=limit)
    vector_doc_hits = [
        DocHit(
            doc_id=d["doc_id"],
            content=d["content"],
            score=d["similarity"],
            source_meta=d["source_meta"] if isinstance(d["source_meta"], dict) else {},
        )
        for d in doc_results
        if d["doc_id"] not in graph_doc_id_set
    ]
    doc_hits = graph_doc_hits + vector_doc_hits

    # 5. (그래프 순회는 위 1.5 단계에서 이미 완료)

    # 6. RetrievalContext 패키징 방출
    return RetrievalContext(
        query=query,
        products=final_products,
        graph_paths=graph_paths,
        memory_facts=memory_facts,
        doc_hits=doc_hits,
    )
