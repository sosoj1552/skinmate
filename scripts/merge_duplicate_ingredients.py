"""이미 적재된 중복 성분 행을 병합하는 1회성 마이그레이션 — ingest/crawler.py 는 건드리지 않는다.

배경: ingest/crawler.py 가 제품 성분표에서 성분을 찾을 때 canonical_key 로만 조회해서,
같은 실물 성분이 성분사전(영문 key)과 제품(한글 key, intro='제품 성분표 기반 자동 등록'
placeholder)에 별개 행으로 중복 적재되어 왔다(ingest/normalize_edit.py 참고, 근본 수정안).
이 스크립트는 근본 수정과 별개로, 지금 이미 들어간 데이터를 product_ingredients FK
재연결로 즉시 고쳐서 그래프 2-hop 추론이 실데이터에서 동작하게 만든다.

원본 중복 행(placeholder intro)은 삭제하지 않고 남겨둔다 — 작업자A 검토 후 정리 여부 결정.
FK 재연결 후에는 그래프를 반드시 재적재(populate_graph.py)해야 CONTAINS 엣지가 올바른
canonical_key 로 다시 투영된다.

실행: .venv/Scripts/python.exe scripts/merge_duplicate_ingredients.py
"""

from __future__ import annotations

import os

import psycopg
import structlog

logger = structlog.get_logger()

_PLACEHOLDER_INTRO = "제품 성분표 기반 자동 등록"


def main() -> None:
    db_url = os.getenv(
        "DATABASE_URL",
        "postgresql://skinmate:skinmate-dev-only@localhost:5432/skinmate",
    )
    logger.info("merge_started", db_url=db_url)

    with psycopg.connect(db_url) as conn, conn.cursor() as cur:
        # 중복 후보: placeholder intro 로 자동 생성된 행(dup) 이면서, 같은 name_ko 를
        # 가진 "진짜" 사전 행(correct, placeholder 아님)이 따로 존재하는 경우.
        cur.execute(
            """
            SELECT dup.ingredient_id AS dup_id, correct.ingredient_id AS correct_id,
                   dup.name_ko
            FROM ingredients dup
            JOIN ingredients correct
                ON correct.name_ko = dup.name_ko
                AND correct.ingredient_id <> dup.ingredient_id
                AND correct.intro IS DISTINCT FROM %s
            WHERE dup.intro = %s;
            """,
            (_PLACEHOLDER_INTRO, _PLACEHOLDER_INTRO),
        )
        pairs = cur.fetchall()
        logger.info("duplicate_pairs_found", count=len(pairs))

        repointed = 0
        skipped_conflict = 0
        for dup_id, correct_id, _name_ko in pairs:
            cur.execute(
                "SELECT product_id FROM product_ingredients WHERE ingredient_id = %s;",
                (dup_id,),
            )
            product_ids = [r[0] for r in cur.fetchall()]

            for product_id in product_ids:
                cur.execute(
                    """
                    SELECT 1 FROM product_ingredients
                    WHERE product_id = %s AND ingredient_id = %s;
                    """,
                    (product_id, correct_id),
                )
                if cur.fetchone():
                    # 이미 올바른 성분으로도 연결되어 있으면(둘 다 있던 경우) 중복 행만 제거
                    cur.execute(
                        """
                        DELETE FROM product_ingredients
                        WHERE product_id = %s AND ingredient_id = %s;
                        """,
                        (product_id, dup_id),
                    )
                    skipped_conflict += 1
                    continue

                cur.execute(
                    """
                    UPDATE product_ingredients
                    SET ingredient_id = %s
                    WHERE product_id = %s AND ingredient_id = %s;
                    """,
                    (correct_id, product_id, dup_id),
                )
                repointed += 1

        conn.commit()
        logger.info(
            "merge_finished",
            pairs=len(pairs),
            repointed=repointed,
            already_linked_dedup=skipped_conflict,
        )
        logger.info(
            "next_step_required",
            message="scripts/populate_graph.py 를 다시 실행해 CONTAINS 엣지를 재투영하세요.",
        )


if __name__ == "__main__":
    main()
