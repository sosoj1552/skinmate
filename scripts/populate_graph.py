"""Apache AGE 그래프 온톨로지 전역 지식 적재를 구동하는 CLI 스크립트.

.env 또는 컨테이너 환경 변수를 조회하여 데이터베이스 접속 정보를 자동으로 조립합니다.
"""

from __future__ import annotations

import os
import time

import psycopg
import structlog

from skinmate.graph.knowledge_populate import populate_global_knowledge

logger = structlog.get_logger()


def main() -> None:
    # 1. 환경 변수로부터 DB 접속 정보 획득 (.env 재활용 지원)
    user = os.getenv("POSTGRES_USER", "skinmate")
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        logger.error("POSTGRES_PASSWORD is not set")
        exit(1)
    db_name = os.getenv("POSTGRES_DB", "skinmate")
    port = os.getenv("POSTGRES_PORT", "5432")
    host = os.getenv("POSTGRES_HOST", "localhost")  # 기본값 localhost, Docker 컨테이너 내에서는 db

    db_url = f"postgresql://{user}:{password}@{host}:{port}/{db_name}"

    logger.info("db_seeder_started", host=host, port=port, db=db_name, user=user)

    # 레이스 컨디션 방지: RDB에 덤프 데이터가 실제로 로드될 때까지 최대 60초 대기
    logger.info("waiting_for_rdb_data_readiness")
    for _ in range(60):
        try:
            with psycopg.connect(db_url) as conn, conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM products;")
                p_count = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM ingredients;")
                i_count = cur.fetchone()[0]
                # 실데이터(가짜 fixture 제외)가 최소 5개 이상 로드되었는지 확인
                if p_count > 5 and i_count > 5:
                    logger.info("rdb_data_ready", products=p_count, ingredients=i_count)
                    break
        except psycopg.OperationalError as op_err:
            err_msg = str(op_err)
            # 인증 오류(비밀번호 불일치 등) 발생 시 대기 루프를 타지 않고 즉시 에러 종료 (Fail-Fast)
            if "password authentication failed" in err_msg or "authentication failed" in err_msg:
                logger.error("db_auth_failed_terminating_immediately", error=err_msg)
                exit(1)
            # 단순 커넥션 실패(DB 기동 대기 등)는 루프 돌며 계속 대기
            pass
        except Exception:
            pass
        time.sleep(1)
    else:
        logger.error("rdb_data_not_ready_after_60s")
        exit(1)

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
