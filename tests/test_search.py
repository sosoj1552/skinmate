"""유사도 검색 search.py 모듈에 대한 단위 테스트 (WBS 1A.2)."""

from __future__ import annotations

import os

from skinmate.documents.search import search_documents


def test_search_documents_integration() -> None:
    """실제 DB에 쿼리를 날려 pgvector 코사인 유사도 검색이 정상 구동되는지 검증합니다.
    
    기본적으로 stub 모드가 켜진 상태(더미 벡터 반환)에서 SQL 실행 및 응답 포맷을 검증합니다.
    """
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://skinmate:skinmate-dev-only@localhost:5432/skinmate",
    )

    # 1. 속건조 에멀전 검색 수행
    query = "속건조를 해결하는 끈적임 없는 에멀전"
    results = search_documents(db_url, query, limit=3)

    # 2. 결과 검증 (top-3)
    assert len(results) <= 3
    assert len(results) > 0

    for doc in results:
        # 각 필드의 존재 및 타입 검증
        assert "doc_id" in doc
        assert "content" in doc
        assert "similarity" in doc
        assert "source_meta" in doc
        
        assert isinstance(doc["doc_id"], int)
        assert isinstance(doc["content"], str)
        assert isinstance(doc["similarity"], float)
        
        # 코사인 유사도 범위 유효성 검증 [-1.0, 1.0] 내외 (또는 1.0 - distance 이므로 범위가 올바른지)
        assert -2.0 <= doc["similarity"] <= 2.0
