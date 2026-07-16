"""데이터베이스 임베딩 배치 인코더 및 적재 스크립트 (WBS 1A.2).

수집된 제품(products) 및 아티클(documents)의 텍스트 필드를
로컬 bge-m3 모델을 기동하여 실물 1024차원 벡터로 인코딩한 뒤 pgvector 인덱스에 적재합니다.
"""

from __future__ import annotations

import os
import time

import psycopg
import structlog
from dotenv import load_dotenv

# 실물 임베딩 모델 작동 강제 활성화 (스텁 모드 비활성화)
os.environ["SKINMATE_EMBED_STUB"] = "false"

from skinmate.documents.embed import embed_text

logger = structlog.get_logger()


def populate_embeddings(db_url: str) -> None:
    """products 및 documents 테이블의 비어있거나 기존 더미 임베딩을 실물 모델로 갱신합니다."""
    logger.info("starting_batch_embedding_populate_process")

    start_time = time.time()

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            # ── 1. products 임베딩 갱신 ──
            cur.execute("SELECT product_id, name, description FROM products;")
            products = cur.fetchall()
            logger.info("products_fetched_for_embedding", count=len(products))

            for prod_id, name, desc in products:
                desc_text = desc or ""
                if not desc_text.strip():
                    desc_text = "제품 설명 없음"
                text_to_embed = f"제품명: {name}\n설명: {desc_text}"

                logger.info("generating_product_embedding", product_id=prod_id)
                vector = embed_text(text_to_embed)

                cur.execute(
                    """
                    UPDATE products 
                    SET embedding = %s::vector, embedding_model_id = 'bge-m3'
                    WHERE product_id = %s;
                    """,
                    (vector, prod_id),
                )

            # ── 2. documents 임베딩 갱신 ──
            cur.execute("SELECT doc_id, content FROM documents;")
            documents = cur.fetchall()
            logger.info("documents_fetched_for_embedding", count=len(documents))

            for doc_id, content in documents:
                text_to_embed = content or ""
                if not text_to_embed.strip():
                    text_to_embed = "빈 아티클 설명"

                logger.info("generating_document_embedding", doc_id=doc_id)
                vector = embed_text(text_to_embed)

                cur.execute(
                    """
                    UPDATE documents 
                    SET embedding = %s::vector, embedding_model_id = 'bge-m3'
                    WHERE doc_id = %s;
                    """,
                    (vector, doc_id),
                )

        conn.commit()

    duration = time.time() - start_time
    logger.info("batch_embedding_populate_process_completed", duration_seconds=round(duration, 2))


if __name__ == "__main__":
    load_dotenv()

    user = os.getenv("POSTGRES_USER", "skinmate")
    password = os.getenv("POSTGRES_PASSWORD", "")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    dbname = os.getenv("POSTGRES_DB", "skinmate")

    db_url = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"

    populate_embeddings(db_url)
