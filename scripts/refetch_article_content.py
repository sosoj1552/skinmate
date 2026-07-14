"""아티클 본문 재수집 스크립트 — Playwright 렌더링 기반, ingest/crawler.py 는 건드리지 않는다.

배경: ingest/crawler.py(1A.1, 담당 A)의 아티클 수집 로직이 폴라초이스 사이트의 아티클 페이지가
클라이언트사이드 렌더링(SPA)이라는 걸 못 잡아내고, httpx 로 받은 원본 HTML에서 "관련 기사"
위젯 텍스트를 본문으로 잘못 저장해왔다(documents 160건 중 159건이 동일한 placeholder 텍스트).
headless 브라우저(Playwright)로 렌더링한 뒤에는 기존 <p> 태그 추출 방식이 그대로 잘 먹힌다는
것을 확인했다 — 이 스크립트는 그 검증된 방식으로 이미 저장된 documents 행을 URL 기준으로
찾아 본문·임베딩을 갱신한다. 크롤러 본체 수정은 작업자A 와 상의 후 별도로 반영 예정.

사용 전 준비:
  pip install -e ".[crawl]"
  python -m playwright install chromium

실행: .venv/Scripts/python.exe scripts/refetch_article_content.py
"""

from __future__ import annotations

import os
import random
import time

import psycopg
import structlog
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from skinmate.documents.embed import embed_text

logger = structlog.get_logger()

# ingest/crawler.py 를 건드리지 않기 위해, 필요한 상수/함수만 그대로 복제한다(HEADERS,
# delay_request) — scripts/ 에서 파일 직접 실행 시 ingest 패키지가 sys.path 에 없어
# import 도 어차피 안 됨.
CRAWL_RATE_LIMIT = float(os.getenv("CRAWL_RATE_LIMIT", "1.5"))
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}


def delay_request() -> None:
    """차단을 피하기 위해 random 딜레이를 부여합니다(ingest/crawler.py 와 동일 정책)."""
    wait_time = random.uniform(0.7, 1.5) / (CRAWL_RATE_LIMIT / 1.5)
    time.sleep(wait_time)


_JUNK_MARKER = 'highlight color="enlighten"'  # 기존에 잘못 저장된 placeholder 식별용
_MIN_BODY_LEN = 300  # 실제 아티클(1,700자대)과 푸터 전용 오탐(220자대)을 가르는 임계값


def _extract_body_via_render(page, url: str) -> str:
    """Playwright 로 페이지를 렌더링한 뒤, 검증된 <p> 태그 추출로 본문을 뽑는다."""
    page.goto(url, wait_until="networkidle", timeout=30000)
    html = page.content()
    soup = BeautifulSoup(html, "html.parser")
    paragraphs = [p.get_text().strip() for p in soup.find_all("p")]
    return "\n".join([p for p in paragraphs if len(p) > 20])


def main() -> None:
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://skinmate:skinmate-dev-only@localhost:5432/skinmate",
    )
    logger.info("refetch_started", db_url=db_url)

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT doc_id, source_meta->>'url' AS url
                FROM documents
                WHERE content LIKE %s AND source_meta->>'url' IS NOT NULL;
                """,
                (f"%{_JUNK_MARKER}%",),
            )
            targets = cur.fetchall()

        logger.info("junk_documents_identified", count=len(targets))
        if not targets:
            logger.info("nothing_to_refetch")
            return

        fixed = 0
        failed = 0
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(extra_http_headers=HEADERS)

            for doc_id, url in targets:
                delay_request()
                try:
                    body = _extract_body_via_render(page, url)
                except Exception as e:
                    logger.error("article_render_failed", doc_id=doc_id, url=url, error=str(e))
                    failed += 1
                    continue

                # 성분사전/카테고리 허브 페이지는 <p> 태그가 실제 본문이 아니라 푸터
                # 텍스트(회사 정보 등)만 잡힐 수 있다 — 최소 길이로 그런 오탐을 거른다.
                if len(body.strip()) < _MIN_BODY_LEN:
                    logger.warning(
                        "body_too_short_skipped", doc_id=doc_id, url=url, body_len=len(body)
                    )
                    failed += 1
                    continue

                vector = embed_text(body)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE documents
                        SET content = %s, embedding = %s, embedding_model_id = 'bge-m3'
                        WHERE doc_id = %s;
                        """,
                        (body, vector, doc_id),
                    )
                conn.commit()
                fixed += 1
                logger.info("article_refetched", doc_id=doc_id, url=url, body_len=len(body))

            browser.close()

    logger.info("refetch_finished", fixed=fixed, failed=failed)


if __name__ == "__main__":
    start = time.time()
    main()
    logger.info("total_duration_seconds", seconds=round(time.time() - start, 1))
