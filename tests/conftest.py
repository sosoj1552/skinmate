"""pytest 전역 설정 및 비파괴적 테스트 데이터 청소 피스처."""

from __future__ import annotations

import os
import urllib.parse

import psycopg
import pytest
from dotenv import load_dotenv

# pytest 구동 시 로컬 .env 로드
load_dotenv()

# Superuser 접속 정보 (.env의 POSTGRES_* 변수)
_user = os.getenv("POSTGRES_USER", "skinmate")
_pass = os.getenv("POSTGRES_PASSWORD", "")
_host = os.getenv("POSTGRES_HOST", "localhost")
_port = os.getenv("POSTGRES_PORT", "5432")

# superuser로 skinmate DB 접속 (CREATE DATABASE, TRUNCATE 등 관리 작업용)
_admin_url = f"postgresql://{_user}:{_pass}@{_host}:{_port}/skinmate"
# superuser로 skinmate_test DB 접속 (clean_db_fixtures 전체 삭제용)
_admin_test_url = f"postgresql://{_user}:{_pass}@{_host}:{_port}/skinmate_test"


def _ensure_test_db(admin_url: str) -> None:
    """skinmate_test DB가 없으면 skinmate를 template으로 복제 생성.

    TEMPLATE skinmate는 스키마+확장을 통째로 복사하지만, 실데이터(memories 등)도 함께 복사됩니다.
    테스트는 빈 상태에서 시작해야 하므로 생성 직후 user-data 테이블을 truncate합니다.
    """
    try:
        with psycopg.connect(admin_url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = 'skinmate_test'")
            if cur.fetchone():
                return
            # TEMPLATE 복사 전 skinmate DB 연결을 모두 끊어야 함
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'skinmate' AND pid <> pg_backend_pid()"
            )
            cur.execute("CREATE DATABASE skinmate_test TEMPLATE skinmate")

        # 생성 직후 실데이터 제거 — 테스트는 빈 상태에서 시작해야 함
        # rsplit을 사용해 URL username 부분을 치환하지 않도록 주의
        test_url = admin_url.rsplit("/", 1)[0] + "/skinmate_test"
        with psycopg.connect(test_url, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("TRUNCATE memories, memory_audit RESTART IDENTITY CASCADE")
    except Exception:
        # DB 연결 실패 시 무시 — 개별 테스트에서 skip 처리됨
        pass


_ensure_test_db(_admin_url)

# DATABASE_URL: 원본 .env URL의 DB명만 skinmate_test로 교체하여 app user(RLS 적용) 유지
_original_db_url = os.getenv("DATABASE_URL", "")
if _original_db_url:
    _parsed = urllib.parse.urlparse(_original_db_url)
    _app_test_url = _parsed._replace(path="/skinmate_test").geturl()
else:
    # fallback: DATABASE_URL이 없으면 config.py 기본값과 동일한 skinmate_app 역할로 접속.
    # superuser(_admin_test_url)로 fallback하면 BYPASSRLS 때문에 RLS 격리 테스트가
    # 전부 무의미해진다(AC-M5 검증 불가) — 반드시 비-superuser 역할을 써야 한다.
    _app_test_url = f"postgresql://skinmate_app:skinmate-app-dev-only@{_host}:{_port}/skinmate_test"

os.environ["DATABASE_URL"] = _app_test_url


@pytest.fixture(name="db_conn")
def fixture_db_conn():
    """테스트용 DB 연결 피스처. superuser 권한으로 직접 접속 및 테스트 후 자동 롤백.

    skinmate_app(비수퍼유저)는 RLS 정책에 걸려 직접 INSERT/SELECT가 제한되므로,
    스키마 조작이 필요한 integration test에서는 superuser 연결을 사용합니다.
    """
    try:
        with psycopg.connect(_admin_test_url, autocommit=False) as conn:
            yield conn
            conn.rollback()
    except psycopg.OperationalError:
        pytest.skip("database connection failed, skipping test.")


@pytest.fixture(scope="function", autouse=True)
def clean_db_fixtures():
    """테스트용 RDB 데이터 및 User 노드들을 비파괴적으로 청소합니다."""

    def _clean() -> None:
        try:
            # superuser로 접속하여 RLS 우회 후 전체 청소
            with psycopg.connect(_admin_test_url) as conn, conn.cursor() as cur:
                # 1. memories/memory_audit 전체 삭제
                # (skinmate_test는 실데이터 없는 테스트 전용 DB — 전체 삭제가 안전)
                cur.execute("DELETE FROM memory_audit;")
                cur.execute("DELETE FROM memories;")

                # 2. 테스트용 junction 및 제품 데이터 물리 삭제
                cur.execute("""
                    DELETE FROM product_ingredients
                    WHERE product_id IN (
                        SELECT product_id FROM products WHERE name LIKE '테스트 %'
                    );
                    """)
                cur.execute("DELETE FROM products WHERE name LIKE '테스트 %';")

                # 3. 테스트용 성분 데이터 물리 삭제
                cur.execute("DELETE FROM ingredients WHERE canonical_key LIKE 'test_%';")

                # 4. 테스트용 문서 데이터 물리 삭제
                cur.execute("DELETE FROM documents WHERE source_meta->>'url' = 'test_source';")

                # 5. 그래프 내의 테스트용 User 노드들 청소 (비파괴적)
                cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'skinmate';")
                if cur.fetchone():
                    cur.execute("SET search_path = ag_catalog, public;")
                    cur.execute(
                        "SELECT * FROM cypher('skinmate', $$"
                        "MATCH (u:User) "
                        "WHERE u.user_id >= 990000 AND u.user_id <= 999999 "
                        "DETACH DELETE u"
                        "$$) AS (result agtype);"
                    )
                conn.commit()
        except psycopg.OperationalError:
            pass
        except Exception:
            pass

    # 테스트 시작 전 클리닝
    _clean()
    yield
    # 테스트 종료 후 클리닝
    _clean()
