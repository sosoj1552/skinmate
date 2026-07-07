-- 001_core_knowledge.sql
-- 코어 구조화 지식 (진실원). 담당 A.
-- 근거: docs/DATA-MODEL.md (2026-07-07 재설계 확정본).
-- 규칙: freeze 후 수정 금지 — 변경은 새 번호 마이그레이션으로만.
--
-- [재설계 요지]
--  * concerns/seasons/season_concerns/data_sources 테이블 삭제.
--    - 고민(concern)·계절(season)은 개인 맥락 → memories 로(002).
--    - 성분↔고민(TREATS), 성분↔성분(HELPS)은 테이블 없이 그래프 네이티브(003, 인제스트가 직접 적재).
--    - 출처 메타는 각 행 source_meta(jsonb) 로 흡수.
--  * 임베딩: D_DOC=1024 고정. products/documents 에만.

BEGIN;
SET LOCAL search_path = public;  -- 무자격 객체는 public 에 생성 (ag_catalog 오염 방지)

-- 사용자 (기억/그래프의 소유 주체)
CREATE TABLE users (
    user_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    skin_type   text,                       -- 지성/건성/복합/민감 (nullable)
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- 성분 (성분 정보): 성분명·효과·분류·등급·성분소개. v1 임베딩 없음.
CREATE TABLE ingredients (
    ingredient_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_key   text NOT NULL UNIQUE,      -- INCI 우선, 없으면 정규화 한글명 (중복 병합 열쇠)
    inci_key        text,
    name_ko         text,
    name_en         text,
    grade           text,                      -- 등급
    function        text,                      -- 효과/기능
    classification  text,                      -- 분류
    intro           text,                      -- 성분소개(프로즈)
    source_meta     jsonb NOT NULL DEFAULT '{}'::jsonb,  -- url·kind·crawled_at·robots_ok
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- 제품 (제품 정보): 제품명·브랜드·설명 + 설명 임베딩(제형 신호 근사). brand는 text.
CREATE TABLE products (
    product_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                text NOT NULL,
    brand               text,
    category            text,
    description         text,
    embedding           vector(1024),          -- D_DOC=1024 (bge-m3), 제형 soft-ranking(AC-F1)
    embedding_model_id  text,                  -- 재-임베딩 마이그레이션 대비(R7)
    source_meta         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- 성분-제품 containment (junction, FK). 회피 성분 하드필터(AC-R2)의 진실원. AGE CONTAINS 로 투영.
CREATE TABLE product_ingredients (
    product_id      bigint NOT NULL REFERENCES products(product_id) ON DELETE CASCADE,
    ingredient_id   bigint NOT NULL REFERENCES ingredients(ingredient_id) ON DELETE CASCADE,
    PRIMARY KEY (product_id, ingredient_id)
);
CREATE INDEX product_ingredients_ingredient_idx ON product_ingredients (ingredient_id);

-- 피부관리·성분·제품 프로즈 아티클 (RAG + 그래프 지식 추출 원천)
CREATE TABLE documents (
    doc_id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content             text NOT NULL,
    embedding           vector(1024),
    embedding_model_id  text,
    source_meta         jsonb NOT NULL DEFAULT '{}'::jsonb,  -- url·kind·crawled_at
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- 벡터 유사도 인덱스 (cosine)
CREATE INDEX products_embedding_hnsw  ON products  USING hnsw (embedding vector_cosine_ops);
CREATE INDEX documents_embedding_hnsw ON documents USING hnsw (embedding vector_cosine_ops);

COMMIT;
