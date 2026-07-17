"""GraphRAG "얇은 슬라이스" 프로토타입 (docs/graphrag-design.md §5 W1 메타패스 검증용).

손으로 검증한 여드름 트리플 하나(살리실릭애씨드 --[ACHIEVES]--> 각질제거 --[TREATS]--> acne)를
멱등 적재한 뒤, 질의(여드름) → W1 그래프 순회 → 제품 JOIN(RDB) → 근거 문서 회수까지의
전 과정이 실제로 도는지 콘솔 출력으로 증명한다.

그래프 접근은 choke.age_exec 단일 관문만 경유한다(라벨 생성 DDL만 예외적으로 raw SQL).
재실행해도 안전(MERGE 기반 멱등).

실행: POSTGRES_PASSWORD=change-me PYTHONIOENCODING=utf-8 \
      .venv/Scripts/python.exe scripts/graphrag_slice.py
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
import structlog

from skinmate.graph import choke

logger = structlog.get_logger()

# 승인된 시드 트리플 (사용자 지정, 그대로 적재)
INGREDIENT_ID = 639
INGREDIENT_NAME_KO = "살리실릭애씨드"
MECHANISM_NAME = "각질제거"
CONCERN_NAME = "acne"
CONCERN_LABEL = "트러블"
CONCERN_DISPLAY = "여드름"  # E2E 출력용 사람이 읽는 표현
SOURCE_DOC_IDS = [305]

W1_QUERY = """
MATCH (c:Concern {name: $concern_name})<-[t:TREATS]-(m:Mechanism)<-[a:ACHIEVES]-(i:Ingredient)
RETURN {ing_id: i.ingredient_id, ing_name: i.name_ko, mech: m.name,
        treats_docs: t.source_doc_ids, achieves_docs: a.source_doc_ids}
"""


def _db_url() -> str:
    user = os.getenv("POSTGRES_USER", "skinmate")
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        logger.error("POSTGRES_PASSWORD is not set")
        raise SystemExit(1)
    db_name = os.getenv("POSTGRES_DB", "skinmate")
    port = os.getenv("POSTGRES_PORT", "5432")
    host = os.getenv("POSTGRES_HOST", "localhost")  # 기본값 localhost, Docker 컨테이너 내에서는 db
    return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"


def ensure_labels(conn: psycopg.Connection[Any]) -> None:
    """Mechanism vlabel · ACHIEVES elabel 이 없으면 생성 (03-graph-ontology.sql DO 블록 패턴)."""
    with conn.cursor() as cur:
        cur.execute("SET search_path = ag_catalog, public;")
        cur.execute("""
            DO $$
            DECLARE
                gid oid;
            BEGIN
                SELECT graphid INTO gid FROM ag_catalog.ag_graph WHERE name = 'skinmate';

                IF NOT EXISTS (
                    SELECT 1 FROM ag_catalog.ag_label WHERE name = 'Mechanism' AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_vlabel('skinmate', 'Mechanism');
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM ag_catalog.ag_label WHERE name = 'ACHIEVES' AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_elabel('skinmate', 'ACHIEVES');
                END IF;
            END $$;
            """)
    logger.info("labels_ensured", vlabel="Mechanism", elabel="ACHIEVES")


def seed_triples(conn: psycopg.Connection[Any]) -> None:
    """승인된 여드름 트리플 하나를 멱등 MERGE 로 적재한다.

    ⚠️ AGE/Cypher 결함·함정 두 가지:
    1) 관계 패턴을 단일 MERGE로 묶으면(예: `MERGE (a {...})-[:REL]->(b {...})`) 그 패턴
       전체가 없을 때 이미 존재하는 a·b 노드를 재사용하지 않고 완전히 새 노드를 만들어버린다
       (표준 Cypher MERGE 전체-패턴 매칭 함정). 그래서 노드는 각각 별도 MERGE로 먼저
       바인딩한 뒤, 바인딩된 변수로 관계만 MERGE 해야 한다(knowledge_populate.py/bridge.py
       와 동일 패턴).
    2) MERGE 로 새로 생성된 관계에 곧바로 SET 을 이어붙이면 속성이 영속화되지 않는다
       (노드 속성은 무관, 관계만 해당) — 별도 MATCH+SET 으로 나눈다.
    """
    # 노드 (노드 속성은 같은 MERGE 문에서 SET 해도 안전)
    choke.age_exec(
        conn,
        None,
        "MERGE (i:Ingredient {ingredient_id: $ingredient_id}) SET i.name_ko = $name_ko",
        {"ingredient_id": INGREDIENT_ID, "name_ko": INGREDIENT_NAME_KO},
    )
    choke.age_exec(
        conn,
        None,
        "MERGE (m:Mechanism {name: $name})",
        {"name": MECHANISM_NAME},
    )
    choke.age_exec(
        conn,
        None,
        "MERGE (c:Concern {name: $name}) SET c.label = $label",
        {"name": CONCERN_NAME, "label": CONCERN_LABEL},
    )

    # ACHIEVES 엣지: 노드를 각각 MERGE로 먼저 바인딩한 뒤 관계만 MERGE (존재만 보장)
    choke.age_exec(
        conn,
        None,
        "MERGE (i:Ingredient {ingredient_id: $ingredient_id}) "
        "MERGE (m:Mechanism {name: $mech_name}) "
        "MERGE (i)-[:ACHIEVES]->(m)",
        {"ingredient_id": INGREDIENT_ID, "mech_name": MECHANISM_NAME},
    )
    # ACHIEVES 속성: 별도 MATCH+SET
    choke.age_exec(
        conn,
        None,
        "MATCH (i:Ingredient {ingredient_id: $ingredient_id})"
        "-[r:ACHIEVES]->(m:Mechanism {name: $mech_name}) "
        "SET r.source_doc_ids = $source_doc_ids, r.origin = $origin, r.confidence = $confidence",
        {
            "ingredient_id": INGREDIENT_ID,
            "mech_name": MECHANISM_NAME,
            "source_doc_ids": SOURCE_DOC_IDS,
            "origin": "manual",
            "confidence": 1.0,
        },
    )

    # TREATS 엣지: 노드를 각각 MERGE로 먼저 바인딩한 뒤 관계만 MERGE (존재만 보장)
    choke.age_exec(
        conn,
        None,
        "MERGE (m:Mechanism {name: $mech_name}) "
        "MERGE (c:Concern {name: $concern_name}) "
        "MERGE (m)-[:TREATS]->(c)",
        {"mech_name": MECHANISM_NAME, "concern_name": CONCERN_NAME},
    )
    # TREATS 속성: 별도 MATCH+SET
    choke.age_exec(
        conn,
        None,
        "MATCH (m:Mechanism {name: $mech_name})-[r:TREATS]->(c:Concern {name: $concern_name}) "
        "SET r.source_doc_ids = $source_doc_ids, r.origin = $origin, r.confidence = $confidence",
        {
            "mech_name": MECHANISM_NAME,
            "concern_name": CONCERN_NAME,
            "source_doc_ids": SOURCE_DOC_IDS,
            "origin": "manual",
            "confidence": 1.0,
        },
    )
    logger.info(
        "seed_triples_merged",
        ingredient_id=INGREDIENT_ID,
        mechanism=MECHANISM_NAME,
        concern=CONCERN_NAME,
    )


def traverse_w1(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """W1 메타패스(고민←도움됨←작동원리←수행←성분) 전역 순회 (user_scope=None)."""
    rows = choke.age_exec(conn, None, W1_QUERY, {"concern_name": CONCERN_NAME})
    paths: list[dict[str, Any]] = []
    for row in rows:
        val = row.get("value", row) if isinstance(row, dict) else row
        if not isinstance(val, dict) or "ing_id" not in val:
            continue
        paths.append(val)
    return paths


def fetch_products(
    conn: psycopg.Connection[Any], ingredient_ids: list[int]
) -> list[tuple[int, str, str | None]]:
    """수집된 ingredient_id 를 함유한 제품을 RDB JOIN 으로 조회한다."""
    if not ingredient_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.product_id, p.name, p.brand
            FROM products p
            JOIN product_ingredients pi ON pi.product_id = p.product_id
            WHERE pi.ingredient_id = ANY(%s);
            """,
            (ingredient_ids,),
        )
        return cur.fetchall()


def fetch_documents(conn: psycopg.Connection[Any], doc_ids: list[int]) -> dict[int, str]:
    """근거 문서(source_doc_ids) 발췌를 회수한다."""
    if not doc_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT doc_id, left(content, 200) FROM documents WHERE doc_id = ANY(%s);",
            (doc_ids,),
        )
        return dict(cur.fetchall())


def main() -> None:
    db_url = _db_url()
    logger.info("graphrag_slice_started")

    with psycopg.connect(db_url) as conn:
        ensure_labels(conn)
        seed_triples(conn)
        conn.commit()

        paths = traverse_w1(conn)

        ingredient_ids: set[int] = set()
        doc_ids: set[int] = set()
        for p in paths:
            ingredient_ids.add(int(p["ing_id"]))
            for docs_key in ("treats_docs", "achieves_docs"):
                docs = p.get(docs_key)
                if docs:
                    doc_ids.update(int(d) for d in docs)

        products = fetch_products(conn, sorted(ingredient_ids))
        documents = fetch_documents(conn, sorted(doc_ids))

    # --- E2E 출력 ---
    print(f"질문: {CONCERN_DISPLAY}에 좋은 제품 추천")

    if not paths:
        print("추천 경로(이유): 0건 — 그래프 순회 결과 없음 (디버깅 필요)")
    for p in paths:
        print(
            f"추천 경로(이유): {CONCERN_DISPLAY} ← [{p['mech']}가 도움] ← {p['mech']} "
            f"← [{p['ing_name']}가 수행] ← {p['ing_name']}"
        )

    if not documents:
        print("근거 문서: 0건 — 문서를 찾을 수 없음 (디버깅 필요)")
    for doc_id in sorted(documents):
        print(f'근거 문서: doc {doc_id} — "{documents[doc_id]}..."')

    print(f"추천 제품({INGREDIENT_NAME_KO} 함유, {len(products)}개):")
    if not products:
        print("  (제품 0건 — 디버깅 필요)")
    for product_id, name, brand in products:
        print(f"  - product_id={product_id} {name} ({brand})")


if __name__ == "__main__":
    main()
