"""Apache AGE 그래프 온톨로지 전역 지식 적재를 구동하는 CLI 스크립트.

.env 또는 컨테이너 환경 변수를 조회하여 데이터베이스 접속 정보를 자동으로 조립합니다.
"""

from __future__ import annotations

import os

import psycopg
import structlog

from skinmate.graph.knowledge_populate import populate_global_knowledge

logger = structlog.get_logger()


def main() -> None:
    # 1. 환경 변수로부터 DB 접속 정보 획득 (.env 재활용 지원)
    user = os.getenv("POSTGRES_USER", "skinmate")
    password = os.getenv("POSTGRES_PASSWORD", "skinmate-dev-only")
    db_name = os.getenv("POSTGRES_DB", "skinmate")
    port = os.getenv("POSTGRES_PORT", "5432")
    host = os.getenv("POSTGRES_HOST", "localhost")  # 기본값 localhost, Docker 컨테이너 내에서는 db

    db_url = f"postgresql://{user}:{password}@{host}:{port}/{db_name}"

    logger.info("db_seeder_started", host=host, port=port, db=db_name, user=user)

    try:
        with psycopg.connect(db_url) as conn:
            populate_global_knowledge(conn)
            conn.commit()
        logger.info("db_seeder_completed_successfully")
    except Exception as e:
        logger.error("db_seeder_failed", error=str(e))
        exit(1)


if __name__ == "__main__":
    main()
