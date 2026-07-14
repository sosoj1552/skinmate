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
from skinmate.contracts.refs import ProductRef
from skinmate.contracts.retrieval import DocHit, RetrievalContext
from skinmate.documents.embed import embed_text
from skinmate.documents.search import search_documents
from skinmate.graph.traverse import traverse_recommendation_paths
from skinmate.memory.rank import rank_memory


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
    avoid_ingredient_ids = [
        fact.target_ingredient_id
        for fact in memory_facts
        if fact.fact_type == FactType.AVOID_INGREDIENT and fact.target_ingredient_id is not None
    ]

    # 2. pgvector 기반 제품 코사인 유사도 검색 수행
    query_vector = embed_text(query)

    # 쿼리 조립 및 기피성분 하드필터링
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

    # 4. RAG 문서 유사도 검색 실행
    doc_results = search_documents(settings.database_url, query, limit=limit)
    doc_hits = [
        DocHit(
            doc_id=d["doc_id"],
            content=d["content"],
            score=d["similarity"],
            source_meta=d["source_meta"] if isinstance(d["source_meta"], dict) else {},
        )
        for d in doc_results
    ]

    # 5. 그래프 2-hop 추론 경로 순회
    graph_paths = traverse_recommendation_paths(conn, user_id, season=season)

    # 6. RetrievalContext 패키징 방출
    return RetrievalContext(
        query=query,
        products=final_products,
        graph_paths=graph_paths,
        memory_facts=memory_facts,
        doc_hits=doc_hits,
    )
