"""폴라초이스 공식 몰 크롤러 및 적재 엔진 (WBS 1A.1).

성분사전, 제품 전성분을 안전하고 정중하게 수집하여
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
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

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

    # ── [A-1] 전제품 목록 페이지에서 Playwright 무한 스크롤로 제품 URL 추출 ──
    logger.info("fetching_product_urls_via_infinite_scroll")
    list_url = "https://www.paulaschoice.co.kr/skin-care-products"
    product_urls = []

    with sync_playwright() as p:
        logger.info("launching_headless_browser_for_product_urls")
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080})

        try:
            page.goto(list_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)  # 팝업 로딩 대기

            # 팝업 닫기 시도
            close_btn_selectors = [
                "//*[text()='닫기']",
                "//*[text()='하루 동안 열지 않기']",
                "//*[contains(@class, 'CloseModalButton')]",
                "//*[contains(@class, 'close')]",
            ]
            for sel in close_btn_selectors:
                try:
                    btn = page.locator(f"xpath={sel}").first
                    if btn.is_visible():
                        btn.click()
                        logger.info("popup_closed_on_product_list_page")
                        page.wait_for_timeout(2000)
                        break
                except Exception:
                    pass

            sentinel_selector = '[class*="InfiniteScroll__Sentinel"]'
            page.wait_for_selector(sentinel_selector, timeout=10000)

            previous_count = 0
            tiles = page.locator('[class*="ProductList__Tile"]').all()
            current_count = len(tiles)

            scroll_count = 0
            while current_count > previous_count and scroll_count < 10:
                previous_count = current_count
                scroll_count += 1
                try:
                    sentinel = page.locator(sentinel_selector).first
                    sentinel.scroll_into_view_if_needed()
                    page.wait_for_timeout(2500)

                    tiles = page.locator('[class*="ProductList__Tile"]').all()
                    current_count = len(tiles)
                except Exception as e:
                    logger.warn("scrolling_sentinel_failed", error=str(e))
                    break

            # 모든 타일에서 href 추출
            for tile in tiles:
                link = tile.locator("a").first
                href = link.get_attribute("href") if link else None
                if href:
                    if href.startswith("/"):
                        full_url = f"https://www.paulaschoice.co.kr{href}"
                    else:
                        full_url = href
                    product_urls.append(full_url)

            # 중복 제거
            product_urls = list(set(product_urls))
            logger.info("product_urls_collected", count=len(product_urls))
        except Exception as e:
            logger.error("product_list_extraction_failed", error=str(e))
            raise e
        finally:
            browser.close()

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

                def clean_html(text: str) -> str:
                    if not text:
                        return ""
                    txt = BeautifulSoup(text, "html.parser").get_text(separator=" ")
                    txt = re.sub(r"\s+", " ", txt)
                    return txt.strip()

                short_desc = clean_html(product_data.get("shortDescription") or "")
                long_desc = clean_html(product_data.get("longDescription") or "")

                skin_types = product_data.get("skinTypes") or ""
                concerns = product_data.get("concerns") or ""

                desc_parts = []
                if short_desc:
                    desc_parts.append(short_desc)
                if long_desc:
                    desc_parts.append(long_desc)
                if skin_types:
                    desc_parts.append(f"피부 타입: {skin_types}")
                if concerns:
                    desc_parts.append(f"피부 고민: {concerns}")

                full_desc = "\n".join(desc_parts).strip()

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
        conn.commit()
    logger.info("crawl_and_seed_process_finished")


if __name__ == "__main__":
    load_dotenv()

    user = os.getenv("POSTGRES_USER", "skinmate")
    password = os.getenv("POSTGRES_PASSWORD", "")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    dbname = os.getenv("POSTGRES_DB", "skinmate")

    db_url = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"

    seed_ingredients_and_products(db_url)
