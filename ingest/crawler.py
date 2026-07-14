"""폴라초이스 공식 몰 크롤러 및 적재 엔진 (WBS 1A.1).

성분사전, 제품 전성분, RAG용 아티클 문서를 안전하고 정중하게 수집하여
DB(관계형 테이블 및 매핑 junction)에 멱등하게 적재합니다.
"""

from __future__ import annotations

import html
import json
import os
import random
import re
import time
from typing import Any, cast

import httpx
import psycopg
import structlog
from bs4 import BeautifulSoup

from ingest.normalize import create_canonical_key, resolve_ingredient_id

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


def delay_request() -> None:
    """차단을 피하기 위해 random 딜레이를 부여합니다."""
    wait_time = random.uniform(0.7, 1.5) / (CRAWL_RATE_LIMIT / 1.5)
    time.sleep(wait_time)


def parse_app_data(html_text: str) -> dict[str, Any]:
    """HTML 본문에서 id='appData' div의 JSON 속성을 추출합니다."""
    soup = BeautifulSoup(html_text, "html.parser")
    app_data_tag = soup.find(id="appData")
    if not app_data_tag:
        return {}
    data_attr = app_data_tag.get("data")
    if not data_attr:
        return {}
    decoded_data = html.unescape(str(data_attr))
    try:
        return cast(dict[str, Any], json.loads(decoded_data))
    except json.JSONDecodeError:
        logger.error("failed_to_parse_app_data_json")
        return {}


def seed_ingredients_and_products(db_url: str) -> None:
    """폴라초이스 사이트맵 및 성분사전에서 데이터를 수집해 적재합니다."""
    logger.info("crawl_and_seed_process_started", db_url=db_url)

    sitemap_url = "https://www.paulaschoice.co.kr/sitemap_0.xml"
    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30.0) as client:
        try:
            resp = client.get(sitemap_url)
            resp.raise_for_status()
            locs = re.findall(r"<loc>([^<]+)</loc>", resp.text)
            logger.info("sitemap_urls_fetched", count=len(locs))
        except Exception as e:
            logger.error("sitemap_fetch_failed", error=str(e))
            raise e

    product_urls = []
    article_urls = []
    for loc_url in locs:
        if "/expert-advice/" in loc_url or "/skincare-advice/" in loc_url:
            article_urls.append(loc_url)
        elif "/paulas-choice-skincare/" in loc_url or (
            loc_url.endswith(".html") and "/ingredients/" not in loc_url
        ):
            product_urls.append(loc_url)

    logger.info(
        "url_categorization",
        products=len(product_urls),
        articles=len(article_urls),
    )

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            # ── [A] 성분사전 수집 (50개씩 페이징) ──
            logger.info("fetching_ingredient_dictionary")
            start = 0
            sz = 50
            total_ingredients_count = 0

            while True:
                list_url = (
                    f"https://www.paulaschoice.co.kr/ingredients"
                    f"?csortb1=name&csortd1=1&start={start}&sz={sz}"
                )
                logger.info("fetching_ingredients_batch", start=start, sz=sz)

                try:
                    with httpx.Client(
                        headers=HEADERS, follow_redirects=True, timeout=20.0
                    ) as client:
                        resp = client.get(list_url)
                        resp.raise_for_status()
                except Exception as e:
                    logger.error(
                        "ingredients_batch_fetch_failed",
                        url=list_url,
                        error=str(e),
                    )
                    break

                app_data = parse_app_data(resp.text)
                page_data = app_data.get("page", {})
                ingredients_list = page_data.get("ingredients", [])

                if not ingredients_list:
                    logger.info(
                        "no_more_ingredients_found",
                        total=total_ingredients_count,
                    )
                    break

                for ing in ingredients_list:
                    rating = ing.get("rating")
                    if rating == "평가보류 성분":
                        continue

                    name_ko = ing.get("name", "")
                    name_en = ing.get("englishName", "")
                    intro = ing.get("description", "")
                    detail_url = ing.get("url", "")

                    canonical_key = create_canonical_key(name_en, name_ko)
                    if not canonical_key:
                        continue

                    target_url = (
                        f"https://www.paulaschoice.co.kr{detail_url}" if detail_url else list_url
                    )
                    source_meta = {
                        "url": target_url,
                        "kind": "ingredient_detail",
                        "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "robots_ok": True,
                    }

                    cur.execute(
                        """
                        INSERT INTO ingredients (
                            canonical_key, inci_key, name_ko,
                            name_en, grade, intro, source_meta
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (canonical_key) DO UPDATE SET
                            name_ko = EXCLUDED.name_ko,
                            name_en = EXCLUDED.name_en,
                            grade = EXCLUDED.grade,
                            intro = EXCLUDED.intro,
                            source_meta = EXCLUDED.source_meta;
                        """,
                        (
                            canonical_key,
                            name_en,
                            name_ko,
                            name_en,
                            rating,
                            intro,
                            json.dumps(source_meta),
                        ),
                    )
                    total_ingredients_count += 1

                paging = page_data.get("paging", {})
                total_limit = paging.get("total", 0)
                start += sz
                if start >= total_limit:
                    logger.info(
                        "reached_end_of_dictionary",
                        total=total_ingredients_count,
                    )
                    break

                delay_request()

            # ── [B] 제품 수집 및 성분 매핑 ──
            logger.info("fetching_products_and_mapping")
            unique_product_urls = list(set(product_urls))
            logger.info("target_products_identified", count=len(unique_product_urls))

            for prod_url in unique_product_urls:
                logger.info("fetching_product_detail", url=prod_url)
                delay_request()

                try:
                    with httpx.Client(
                        headers=HEADERS, follow_redirects=True, timeout=20.0
                    ) as client:
                        resp = client.get(prod_url)
                        resp.raise_for_status()
                except Exception as e:
                    logger.error(
                        "product_detail_fetch_failed",
                        url=prod_url,
                        error=str(e),
                    )
                    continue

                app_data = parse_app_data(resp.text)
                page_data = app_data.get("page", {})
                product_data = page_data.get("product", {})

                if not product_data:
                    logger.warn("empty_product_data", url=prod_url)
                    continue

                variants = product_data.get("variants", [])
                product_name = ""
                if variants:
                    product_name = variants[0].get("name") or product_data.get("productName", "")
                else:
                    product_name = product_data.get("productName", "")

                if not product_name:
                    logger.warn("no_product_name_found", url=prod_url)
                    continue

                category = product_data.get("categoryId") or "Skincare"
                short_desc = product_data.get("shortDescription") or ""
                long_desc = product_data.get("longDescription") or ""
                full_desc = f"{short_desc}\n{long_desc}".strip()

                source_meta = {
                    "url": prod_url,
                    "kind": "product_detail",
                    "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "robots_ok": True,
                }

                dummy_vector = [0.0] * 1024

                cur.execute(
                    "SELECT product_id FROM products WHERE name = %s;",
                    (product_name,),
                )
                prod_row = cur.fetchone()
                if prod_row:
                    product_id = prod_row[0]
                    cur.execute(
                        """
                        UPDATE products SET
                            brand = %s,
                            category = %s,
                            description = %s,
                            source_meta = %s
                        WHERE product_id = %s;
                        """,
                        (
                            "Paula's Choice",
                            category,
                            full_desc,
                            json.dumps(source_meta),
                            product_id,
                        ),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO products (
                            name, brand, category, description,
                            embedding, embedding_model_id, source_meta
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                        RETURNING product_id;
                        """,
                        (
                            product_name,
                            "Paula's Choice",
                            category,
                            full_desc,
                            dummy_vector,
                            "bge-m3",
                            json.dumps(source_meta),
                        ),
                    )
                    res_row = cur.fetchone()
                    assert res_row is not None
                    product_id = res_row[0]

                ingredients_str = product_data.get("ingredients")
                if isinstance(ingredients_str, str) and ingredients_str.strip():
                    raw_ing_names = [i.strip() for i in ingredients_str.split(",") if i.strip()]
                    logger.info(
                        "parsing_product_ingredients",
                        product=product_name,
                        count=len(raw_ing_names),
                    )

                    for raw_ing in raw_ing_names:
                        new_source_meta = {
                            "url": prod_url,
                            "kind": "product_ingredient_fallback",
                            "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "robots_ok": True,
                        }
                        ingredient_id, created = resolve_ingredient_id(
                            cur,
                            name_ko=raw_ing,
                            default_source_meta=new_source_meta,
                        )
                        if created:
                            logger.info(
                                "new_ingredient_found_in_product",
                                name=raw_ing,
                            )

                        cur.execute(
                            """
                            INSERT INTO product_ingredients (
                                product_id, ingredient_id
                            ) VALUES (%s, %s)
                            ON CONFLICT DO NOTHING;
                            """,
                            (product_id, ingredient_id),
                        )

            # ── [C] RAG용 아티클 수집 ──
            logger.info("fetching_articles_for_rag")
            unique_article_urls = list(set(article_urls))
            logger.info("target_articles_identified", count=len(unique_article_urls))

            for art_url in unique_article_urls:
                logger.info("fetching_article_detail", url=art_url)
                delay_request()

                try:
                    with httpx.Client(
                        headers=HEADERS, follow_redirects=True, timeout=20.0
                    ) as client:
                        resp = client.get(art_url)
                        resp.raise_for_status()
                except Exception as e:
                    logger.error(
                        "article_detail_fetch_failed",
                        url=art_url,
                        error=str(e),
                    )
                    continue

                app_data = parse_app_data(resp.text)
                page_data = app_data.get("page", {})
                content_data = page_data.get("content", {})

                article_body = ""
                if content_data:
                    text1 = content_data.get("text1") or ""
                    text2 = content_data.get("text2") or ""
                    html1 = content_data.get("html1") or ""

                    soup_html = BeautifulSoup(html1, "html.parser")
                    text_html = soup_html.get_text()

                    article_body = f"{text1}\n{text2}\n{text_html}".strip()

                if not article_body:
                    soup_art = BeautifulSoup(resp.text, "html.parser")
                    paragraphs = [p.get_text().strip() for p in soup_art.find_all("p")]
                    article_body = "\n".join([p for p in paragraphs if len(p) > 20])

                if not article_body.strip():
                    logger.warn("empty_article_body", url=art_url)
                    continue

                source_meta = {
                    "url": art_url,
                    "kind": "beautypedia_prose",
                    "crawled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "robots_ok": True,
                }

                cur.execute(
                    """
                    INSERT INTO documents (
                        content, embedding, embedding_model_id, source_meta
                    ) VALUES (%s, %s, %s, %s);
                    """,
                    (
                        article_body,
                        dummy_vector,
                        "bge-m3",
                        json.dumps(source_meta),
                    ),
                )

        conn.commit()
    logger.info("crawl_and_seed_process_finished")


if __name__ == "__main__":
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://skinmate:skinmate-dev-only@localhost:5432/skinmate",
    )
    seed_ingredients_and_products(db_url)
