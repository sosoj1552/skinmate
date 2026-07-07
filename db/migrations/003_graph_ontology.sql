-- 003_graph_ontology.sql
-- AGE 관계 그래프 온톨로지. 담당 A.
-- 근거: consensus-plan §4 AGE graph ontology, PRD.md F4.
--
-- [설계 결정]
--  * 그래프 이름 = 'skinmate' (단일 그래프).
--  * 노드: User, Ingredient, Product, Concern, Brand. **Formulation·Season 노드 없음**
--    (제형=임베딩 전용, 계절=memories.season 개인 맥락).
--  * 전역 엣지: CONTAINS(product_ingredients 투영) + TREATS/AGGRAVATES·HELPS/CONFLICTS
--    (**그래프 네이티브** — 테이블 없이 인제스트가 문서·크롤에서 추출해 직접 적재).
--  * 개인 엣지: HAS_CONCERN/AVOIDS/PREFERS (memories 투영). Concern/Brand 노드는 name 키.
--  * 개인 엣지는 user_scope 프로퍼티로 스탬프 → choke(age_exec)가 스코프 강제(RLS 대체, R4).

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- 그래프 + 라벨을 멱등(idempotent)하게 생성. 재실행해도 안전.
DO $$
DECLARE
    g        name := 'skinmate';
    gid      oid;
    lbl      text;
    vlabels  text[] := ARRAY['User','Ingredient','Product','Concern','Brand'];
    elabels  text[] := ARRAY['CONTAINS','TREATS','AGGRAVATES','HELPS',
                             'CONFLICTS','HAS_CONCERN','AVOIDS','PREFERS'];
BEGIN
    IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_graph WHERE name = g) THEN
        PERFORM ag_catalog.create_graph(g);
    END IF;

    SELECT graphid INTO gid FROM ag_catalog.ag_graph WHERE name = g;

    FOREACH lbl IN ARRAY vlabels LOOP
        IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_label WHERE name = lbl AND graph = gid) THEN
            PERFORM ag_catalog.create_vlabel(g, lbl);
        END IF;
    END LOOP;

    FOREACH lbl IN ARRAY elabels LOOP
        IF NOT EXISTS (SELECT 1 FROM ag_catalog.ag_label WHERE name = lbl AND graph = gid) THEN
            PERFORM ag_catalog.create_elabel(g, lbl);
        END IF;
    END LOOP;
END $$;
