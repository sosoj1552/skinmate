"""소량 공용 샘플 데이터 적재 스크립트.

WBS 0.5 조기 납품. 관계형 6테이블, pgvector, Apache AGE 그래프에
최소한의 가짜 샘플 데이터를 적재합니다.
"""

from __future__ import annotations

import os

import psycopg


def main() -> None:
    db_url = os.getenv(
        "DATABASE_URL", "postgresql://skinmate:skinmate-dev-only@localhost:5432/skinmate"
    )
    print(f"Connecting to database at {db_url}...")

    # superuser 권한으로 데이터 적재 수행 (memories RLS 우회 및 drop_graph 등 DDL 권한 필요)
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            # ── 1. 기존 데이터 초기화 ──────────────────────────────────────
            print("1. Cleaning up existing data...")
            cur.execute("SET LOCAL search_path = public;")
            cur.execute(
                "TRUNCATE TABLE memories, memory_audit, product_ingredients, "
                "ingredients, products, documents CASCADE;"
            )

            # Apache AGE 그래프 초기화 (존재하면 삭제 후 온톨로지 재생성)
            cur.execute("SET LOCAL search_path = ag_catalog, public;")
            cur.execute("SELECT count(*) FROM ag_graph WHERE name = 'skinmate';")
            if cur.fetchone()[0] > 0:
                cur.execute("SELECT drop_graph('skinmate', true);")

            # 003 마이그레이션과 일치하게 그래프 및 라벨 생성
            cur.execute("SELECT create_graph('skinmate');")
            vlabels = ["User", "Ingredient", "Product", "Concern", "Brand"]
            elabels = [
                "CONTAINS",
                "TREATS",
                "AGGRAVATES",
                "HELPS",
                "CONFLICTS",
                "HAS_CONCERN",
                "AVOIDS",
                "PREFERS",
            ]
            for lbl in vlabels:
                cur.execute(f"SELECT create_vlabel('skinmate', '{lbl}');")
            for lbl in elabels:
                cur.execute(f"SELECT create_elabel('skinmate', '{lbl}');")

            # ── 2. 관계형 데이터 적재 ──────────────────────────────────────
            print("2. Seeding relational tables...")
            cur.execute("SET LOCAL search_path = public;")

            # 가. 성분 (ingredients)
            cur.execute("""
                INSERT INTO ingredients (
                    canonical_key, name_ko, name_en, 
                    inci_key, grade, function, classification
                )
                VALUES 
                (
                    'hyaluronic_acid', '히알루론산', 'Hyaluronic Acid', 
                    'hyaluronic acid', 'Good', '보습제', '보습'
                ),
                (
                    'retinol', '레티놀', 'Retinol', 'retinol', 
                    'Best', '피부컨디셔닝제', '안티에이징'
                ),
                ('alcohol', '에탄올', 'Alcohol', 'alcohol', 'Poor', '용제', '알코올')
                RETURNING canonical_key, ingredient_id;
                """)
            ing_map = {row[0]: row[1] for row in cur.fetchall()}

            # 나. 제품 (products)
            dummy_vector = [0.0] * 1024
            emulsion_desc = (
                "끈적임 없는 가벼운 제형의 고보습 에멀전. "
                "히알루론산이 풍부하게 함유되어 속건조를 해결."
            )
            retinol_desc = "주름 개선을 돕는 고농축 레티놀 세럼. " "민감성 피부는 자극 주의."
            cur.execute(
                """
                INSERT INTO products (
                    name, brand, category, description, embedding, embedding_model_id
                )
                VALUES 
                ('수분 에멀전', 'coos', '에멀전', %s, %s, 'bge-m3'),
                ('레티놀 0.1 세럼', 'paulas-choice', '세럼', %s, %s, 'bge-m3')
                RETURNING name, product_id;
                """,
                (emulsion_desc, dummy_vector, retinol_desc, dummy_vector),
            )
            prod_map = {row[0]: row[1] for row in cur.fetchall()}

            # 다. 성분-제품 junction (product_ingredients)
            cur.execute(f"""
                INSERT INTO product_ingredients (product_id, ingredient_id) VALUES 
                ({prod_map['수분 에멀전']}, {ing_map['hyaluronic_acid']}),
                ({prod_map['레티놀 0.1 세럼']}, {ing_map['retinol']}),
                ({prod_map['레티놀 0.1 세럼']}, {ing_map['alcohol']});
                """)

            # 라. RAG 참고 문서 (documents)
            doc_content = (
                "가을철 환절기에는 대기 중 습도가 급격히 낮아져 피부 장벽이 약화되고 속건조가 "
                "심해집니다. 이 시기에는 히알루론산과 같이 수분을 강력하게 끌어당기는 성분이 "
                "함유된 에멀전 제형을 사용하는 것이 좋습니다. 반면, 무거운 페이스 오일은 "
                "민감하거나 지성 피부에 끈적임과 자극을 유발할 수 있으므로 피하는 것이 안전합니다."
            )
            cur.execute(
                """
                INSERT INTO documents (content, embedding, embedding_model_id, source_meta)
                VALUES (
                    %s, %s, 'bge-m3',
                    '{"url": "https://example.com/skincare/autumn-guide", '
                    '"kind": "beautypedia_prose", '
                    '"crawled_at": "2026-06-30T10:00:00Z", '
                    '"robots_ok": true}'::jsonb
                );
                """,
                (doc_content, dummy_vector),
            )

            # 마. 개인 기억 (memories)
            retinol_avoid_content = "레티놀 성분을 바르면 자극이 있고 " "붉어져서 피하고 싶어요"
            cur.execute(
                f"""
                INSERT INTO memories (
                    user_id, content, fact_type, target_ingredient_id, target_name, season
                ) VALUES
                (1001, '가을철 건조함이 고민이에요', 'has_concern', NULL, 'dryness', '가을'),
                (
                    1001, %s, 
                    'avoid_ingredient', {ing_map['retinol']}, NULL, NULL
                ),
                (1002, '피부가 얇고 아주 민감해요', 'skin_type', NULL, NULL, NULL),
                (
                    1002, '히알루론산이 들어간 제품은 촉촉하고 잘 맞아요', 
                    'prefer_ingredient', {ing_map['hyaluronic_acid']}, NULL, NULL
                );
                """,
                (retinol_avoid_content,),
            )

            # ── 3. 그래프 데이터 적재 (AGE Cypher) ─────────────────────────────
            print("3. Seeding AGE graph nodes and edges...")
            cur.execute("SET LOCAL search_path = ag_catalog, public;")

            # 노드 생성
            cur.execute("""
                SELECT * FROM cypher('skinmate', $$
                    CREATE (i1:Ingredient {canonical_key: 'hyaluronic_acid', name: '히알루론산'})
                    CREATE (i2:Ingredient {canonical_key: 'retinol', name: '레티놀'})
                    CREATE (i3:Ingredient {canonical_key: 'alcohol', name: '에탄올'})
                    CREATE (p1:Product {product_id: 10, name: '수분 에멀전'})
                    CREATE (p2:Product {product_id: 11, name: '레티놀 0.1 세럼'})
                    CREATE (c1:Concern {name: 'dryness', label: '건조'})
                $$) AS (a agtype);
                """)

            # 전역 지식 엣지 생성
            cur.execute("""
                SELECT * FROM cypher('skinmate', $$
                    MATCH (p1:Product {product_id: 10}), 
                          (i1:Ingredient {canonical_key: 'hyaluronic_acid'})
                    MATCH (p2:Product {product_id: 11}), 
                          (i2:Ingredient {canonical_key: 'retinol'})
                    MATCH (p3:Product {product_id: 11}), 
                          (i3:Ingredient {canonical_key: 'alcohol'})
                    MATCH (i1_t:Ingredient {canonical_key: 'hyaluronic_acid'}), 
                          (c_d:Concern {name: 'dryness'})
                    MATCH (i2_t:Ingredient {canonical_key: 'retinol'}), 
                          (c_d2:Concern {name: 'dryness'})
                    
                    CREATE (p1)-[:CONTAINS]->(i1)
                    CREATE (p2)-[:CONTAINS]->(i2)
                    CREATE (p2)-[:CONTAINS]->(i3)
                    CREATE (i1_t)-[:TREATS]->(c_d)
                    CREATE (i2_t)-[:TREATS]->(c_d2)
                $$) AS (a agtype);
                """)

            # 개인 기억 엣지 생성 (user_scope 주입)
            cur.execute("""
                SELECT * FROM cypher('skinmate', $$
                    MATCH (c:Concern {name: 'dryness'})
                    MATCH (i:Ingredient {canonical_key: 'retinol'})
                    MATCH (i_pref:Ingredient {canonical_key: 'hyaluronic_acid'})
                    
                    CREATE (u1:User {user_id: 1001})
                    CREATE (u2:User {user_id: 1002})
                    
                    CREATE (u1)-[:HAS_CONCERN {season: '가을', user_scope: 1001}]->(c)
                    CREATE (u1)-[:AVOIDS {user_scope: 1001}]->(i)
                    CREATE (u2)-[:PREFERS {user_scope: 1002}]->(i_pref)
                $$) AS (a agtype);
                """)

        conn.commit()
        print("OK: Seeding completed successfully.")


if __name__ == "__main__":
    main()
