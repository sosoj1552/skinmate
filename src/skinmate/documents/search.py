"""문서 유사도 검색 모듈 (WBS 1A.2).

pgvector 의 Cosine Distance 오퍼레이터를 활용하여
임베딩 기반 유사 아티클 문서를 DB 에서 조회합니다.
"""

from __future__ import annotations

from typing import Any

import psycopg
import structlog

from skinmate.documents.embed import embed_text

logger = structlog.get_logger()


def search_documents(db_url: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """검색 질의와 유사도가 높은 문서를 최대 limit개 조회합니다.

    유사도(similarity)는 Cosine Distance를 변환한 1 - (embedding <=> query_vector)를 사용합니다.
    """
    logger.info("searching_documents_via_embedding", query=query, limit=limit)
    query_vector = embed_text(query)

    results = []
    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
                SELECT 
                    doc_id, 
                    content, 
                    1.0 - (embedding <=> %s::vector) AS similarity, 
                    source_meta
                FROM documents
                WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT %s;
                """,
            (query_vector, query_vector, limit),
        )
        rows = cur.fetchall()

        for row in rows:
            results.append(
                {
                    "doc_id": row[0],
                    "content": row[1],
                    "similarity": float(row[2]) if row[2] is not None else 0.0,
                    "source_meta": row[3],
                }
            )

    logger.info("documents_search_completed", results_count=len(results))
    return results
