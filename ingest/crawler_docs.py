"""폴라초이스 공식 몰 RAG용 아티클 문서 전용 크롤러 (WBS 1A.1).

사이트맵에서 아티클 페이지(*.html)만 정밀하게 수집한 뒤,
Playwright(headless 브라우저)로 클라이언트 사이드 렌더링(SPA)된 본문을
H2/H3 헤더 및 글자 수 임계치 기준으로 의미적 분할(Chunking)하여 DB에 멱등하게 적재합니다.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from typing import Any

import httpx
import psycopg
import structlog
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

from skinmate.documents.embed import embed_text

logger = structlog.get_logger()

# ── 상수 및 설정 ───────────────────────────────────────────────────
CRAWL_RATE_LIMIT = float(os.getenv("CRAWL_RATE_LIMIT", "1.5"))
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

# 분할 정책 상수
TARGET_CHUNK_SIZE = 500  # 분할 시 목표 크기
MAX_CHUNK_SIZE = 800  # 분할을 실행할 임계 크기 (800자 이하는 쪼개지 않음)
OVERLAP_SIZE = 150  # 분할 시 중복 영역 크기
MIN_CHUNK_SIZE = 150  # 병합 여부를 판단할 최소 크기


def delay_request() -> None:
    """차단을 피하기 위해 random 딜레이를 부여합니다."""
    wait_time = random.uniform(0.7, 1.5) / (CRAWL_RATE_LIMIT / 1.5)
    time.sleep(wait_time)


def fetch_article_urls() -> list[str]:
    """사이트맵을 조회해 실제 아티클(*.html) 주소만 정밀 수집합니다."""
    sitemap_url = "https://www.paulaschoice.co.kr/sitemap_0.xml"
    logger.info("fetching_sitemap_for_articles", url=sitemap_url)

    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30.0) as client:
        try:
            resp = client.get(sitemap_url)
            resp.raise_for_status()
            locs = re.findall(r"<loc>([^<]+)</loc>", resp.text)
        except Exception as e:
            logger.error("sitemap_fetch_failed", error=str(e))
            raise e

    article_urls = []
    for loc_url in locs:
        # expert-advice 나 skincare-advice를 포함하면서
        # 개별 아티클 페이지인 .html로 끝나는 경우만 포함
        is_article = "/expert-advice/" in loc_url or "/skincare-advice/" in loc_url
        if is_article and loc_url.endswith(".html"):
            article_urls.append(loc_url)

    logger.info(
        "sitemap_articles_filtered",
        total_found=len(locs),
        articles_count=len(article_urls),
    )
    return list(set(article_urls))


def extract_article_blocks(soup: BeautifulSoup) -> list[dict[str, str]]:
    """legacy-hack 내부의 주요 블록들을 순서대로 추출합니다. 중복은 방지합니다."""
    legacy_div = soup.find(class_="legacy-hack")
    if not legacy_div:
        return []

    target_tags = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}
    all_elements = legacy_div.find_all(list(target_tags))

    blocks = []
    for el in all_elements:
        # 중복 방지: 부모 중 수집 대상인 블록 태그가 이미 있다면 스킵
        has_target_ancestor = False
        for parent in el.parents:
            if parent == legacy_div:
                break
            if parent.name in target_tags:
                has_target_ancestor = True
                break

        if has_target_ancestor:
            continue

        text = el.get_text().strip()
        if text:
            blocks.append({"tag": el.name, "text": text})
    return blocks


def split_text_with_overlap(text: str, size: int, overlap: int) -> list[str]:
    """텍스트를 문맥 단절을 최소화하며 크기와 오버랩 기준으로 분할합니다."""
    sentences = re.split(r"(?<=\. )|\n\n", text)
    chunks = []
    current_chunk = []
    current_len = 0

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        current_chunk.append(sentence)
        current_len += len(sentence)

        if current_len >= size:
            chunks.append(" ".join(current_chunk))

            # 오버랩 150자 맞춤 역산
            overlap_chunk = []
            overlap_len = 0
            for sent in reversed(current_chunk):
                if overlap_len + len(sent) <= overlap:
                    overlap_chunk.insert(0, sent)
                    overlap_len += len(sent)
                else:
                    break
            current_chunk = overlap_chunk
            current_len = overlap_len

    if current_chunk:
        last_text = " ".join(current_chunk)
        if len(last_text) < MIN_CHUNK_SIZE and chunks:
            chunks[-1] = chunks[-1] + " " + last_text
        else:
            chunks.append(last_text)

    return chunks


def chunk_article(
    article_title: str, article_url: str, blocks: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """수집된 블록들을 규칙에 따라 헤더 기준 또는 글자 수 기준으로 의미 분할(Chunking)합니다."""
    sections: list[dict[str, Any]] = []
    current_section = {"header": "", "body_lines": []}

    for block in blocks:
        tag = block["tag"]
        text = block["text"]

        if tag.startswith("h"):
            if current_section["body_lines"] or current_section["header"]:
                sections.append(current_section)
            current_section = {"header": text, "body_lines": []}
        elif tag == "li":
            current_section["body_lines"].append(f"- {text}")
        else:
            current_section["body_lines"].append(text)

    if current_section["body_lines"] or current_section["header"]:
        sections.append(current_section)

    final_chunks = []
    has_headers = any(sec["header"] for sec in sections)

    # CASE A: 헤더가 전혀 없는 비구조화 아티클
    if not has_headers:
        full_text = "\n\n".join(sum([sec["body_lines"] for sec in sections], []))
        sub_texts = split_text_with_overlap(full_text, TARGET_CHUNK_SIZE, OVERLAP_SIZE)
        for idx, sub_text in enumerate(sub_texts):
            prefix = f"[{article_title} (Part {idx + 1})]"
            final_chunks.append(
                {
                    "content": f"{prefix}\n{sub_text}",
                    "metadata": {
                        "url": article_url,
                        "chunk_index": idx,
                        "is_chunk": True,
                        "kind": "beautypedia_prose",
                    },
                }
            )
        return final_chunks

    # CASE B: 헤더가 존재하는 구조화 아티클
    for sec_idx, sec in enumerate(sections):
        header = sec["header"]
        body_text = "\n\n".join(sec["body_lines"]).strip()

        if not body_text:
            continue

        prefix = f"[{article_title} - {header}]" if header else f"[{article_title} - 개요]"

        body_len = len(body_text)

        # 800자 이하는 단일 청크 유지
        if body_len <= MAX_CHUNK_SIZE:
            final_chunks.append(
                {
                    "content": f"{prefix}\n{body_text}",
                    "metadata": {
                        "url": article_url,
                        "section_index": sec_idx,
                        "chunk_index": 0,
                        "is_chunk": True,
                        "kind": "beautypedia_prose",
                    },
                }
            )
        # 800자 초과는 500자(오버랩 150자)로 쪼갬
        else:
            sub_texts = split_text_with_overlap(body_text, TARGET_CHUNK_SIZE, OVERLAP_SIZE)
            for idx, sub_text in enumerate(sub_texts):
                part_prefix = f"[{article_title} - {header or '개요'} (Part {idx + 1})]"
                final_chunks.append(
                    {
                        "content": f"{part_prefix}\n{sub_text}",
                        "metadata": {
                            "url": article_url,
                            "section_index": sec_idx,
                            "chunk_index": idx,
                            "is_chunk": True,
                            "kind": "beautypedia_prose",
                        },
                    }
                )

    return final_chunks


def crawl_and_seed_articles(db_url: str) -> None:
    """Playwright 기반으로 렌더링된 본문을 수집하고 의미 분할 후 멱등 적재합니다."""
    logger.info("article_crawl_process_started", db_url=db_url)

    article_urls = fetch_article_urls()
    if not article_urls:
        logger.info("no_articles_to_crawl")
        return

    with psycopg.connect(db_url) as conn, sync_playwright() as p:
        logger.info("launching_headless_browser")
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(extra_http_headers=HEADERS)

        for index, art_url in enumerate(article_urls):
            logger.info(
                "fetching_article_content",
                index=index + 1,
                total=len(article_urls),
                url=art_url,
            )
            delay_request()

            try:
                page.goto(art_url, wait_until="networkidle", timeout=30000)
                html_content = page.content()
            except Exception as e:
                logger.error("article_render_failed", url=art_url, error=str(e))
                continue

            soup = BeautifulSoup(html_content, "html.parser")
            h1_tag = soup.find("h1")
            article_title = h1_tag.get_text().strip() if h1_tag else "알려지지 않은 아티클"

            blocks = extract_article_blocks(soup)
            chunks = chunk_article(article_title, art_url, blocks)
            num_chunks = len(chunks)

            # 1. 예전 크롤러가 수집했던 청크 정보 없는 기존 전체 본문 행 정리
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM documents WHERE source_meta->>'url' = %s "
                    "AND source_meta->>'chunk_index' IS NULL;",
                    (art_url,),
                )

            # 2. 새로운 의미 청크들 멱등 반영 (Upsert)
            for chunk in chunks:
                content = chunk["content"]
                meta = chunk["metadata"]
                chunk_idx = meta["chunk_index"]

                embedding_vector = embed_text(content)

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT doc_id FROM documents 
                        WHERE source_meta->>'url' = %s 
                          AND (source_meta->>'chunk_index')::int = %s;
                        """,
                        (art_url, chunk_idx),
                    )
                    row = cur.fetchone()

                    if row:
                        doc_id = row[0]
                        cur.execute(
                            """
                            UPDATE documents SET
                                content = %s,
                                embedding = %s,
                                embedding_model_id = %s,
                                source_meta = %s
                            WHERE doc_id = %s;
                            """,
                            (
                                content,
                                embedding_vector,
                                "bge-m3",
                                json.dumps(meta),
                                doc_id,
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO documents (
                                content, embedding, embedding_model_id, source_meta
                            ) VALUES (%s, %s, %s, %s);
                            """,
                            (
                                content,
                                embedding_vector,
                                "bge-m3",
                                json.dumps(meta),
                            ),
                        )

            # 3. 새로운 크롤 결과로 생성된 청크 개수보다 많은 예전 인덱스의 찌꺼기 청크들 청소
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM documents 
                    WHERE source_meta->>'url' = %s 
                      AND (source_meta->>'chunk_index')::int >= %s;
                    """,
                    (art_url, num_chunks),
                )

            conn.commit()
            logger.info("article_processed_and_chunked", url=art_url, chunks_count=num_chunks)

        browser.close()

    logger.info("article_crawl_process_finished")


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    user = os.getenv("POSTGRES_USER", "skinmate")
    password = os.getenv("POSTGRES_PASSWORD", "")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    dbname = os.getenv("POSTGRES_DB", "skinmate")

    db_url = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
    crawl_and_seed_articles(db_url)
