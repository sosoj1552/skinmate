-- 002_memory_and_rls.sql
-- 사용자 기억 + 사용자 격리(RLS) + 감사. 담당 B (초안→A 통합, ⭐ 리뷰 필수).
-- 근거: docs/DATA-MODEL.md, PRD.md F1/F5, ACCEPTANCE AC-M1/M5/S1.
--
-- [재설계 요지]
--  * concerns 테이블 삭제 → 고민은 fact_type='has_concern' + target_name(text). 그래프 Concern 노드는 name 키.
--  * 계절 = memories.season(text) 개인 맥락. seasons 테이블 없음.
--  * conversations 테이블 없음(서버 stateless — 클라이언트가 히스토리 전달, 퍼널은 매 턴 재계산).
--  * memory_audit 별도 테이블 유지 + RLS(격리 구멍 방지).
--  * users 테이블 없음: user_id 는 외부 신원 스코프값(FK 아님). 피부타입은 fact_type='skin_type' 기억.
--  * effective_weight = base_weight * exp(-λ*Δdays), λ=0.05/day. 앱에서 조회 시 계산.

BEGIN;
SET LOCAL search_path = public;  -- 무자격 객체는 public 에 생성 (ag_catalog 오염 방지)

-- 기억 종류 (그래프 투영 여부를 가르는 축)
CREATE TYPE fact_type AS ENUM (
    'skin_type',            -- 피부타입(지성/건성/복합/민감). 관계형만, 그래프 투영 안 함
    'avoid_ingredient',     -- → (:User)-[:AVOIDS]->(:Ingredient)   (target_ingredient_id)
    'prefer_ingredient',    -- → (:User)-[:PREFERS]->(:Ingredient)  (target_ingredient_id)
    'avoid_brand',          -- → (:User)-[:AVOIDS]->(:Brand)        (target_name)
    'prefer_brand',         -- → (:User)-[:PREFERS]->(:Brand)       (target_name)
    'has_concern',          -- → (:User)-[:HAS_CONCERN {season?}]->(:Concern)  (target_name)
    'other'                 -- 관계형만, 그래프 투영 안 함
);

-- 개인 사실. RLS 로 user_id 격리.
CREATE TABLE memories (
    memory_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id             bigint NOT NULL,       -- 외부 신원 스코프값(users 테이블 없음, FK 아님). RLS 스코프
    content             text NOT NULL,
    fact_type           fact_type NOT NULL,
    slot_key            text,                  -- 동일 슬롯 update 판정용(예: 'avoid:레티놀')
    season              text,                  -- 계절 맥락(가을/겨울…), has_concern 등에 부가
    base_weight         double precision NOT NULL DEFAULT 1.0,
    frequency           integer NOT NULL DEFAULT 1,
    last_seen           timestamptz NOT NULL DEFAULT now(),
    -- 기억→그래프 다리용 해석 참조
    target_ingredient_id bigint REFERENCES ingredients(ingredient_id) ON DELETE SET NULL,
    target_name          text,                 -- concern/brand 이름(그래프 네이티브 노드 키)
    created_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz            -- soft-delete
);
CREATE INDEX memories_user_idx        ON memories (user_id);
CREATE INDEX memories_user_active_idx ON memories (user_id, fact_type) WHERE deleted_at IS NULL;

-- CRUD 감사 로그 (add/update/delete/no-op, old→new)
CREATE TABLE memory_audit (
    audit_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    memory_id   bigint REFERENCES memories(memory_id) ON DELETE SET NULL,
    user_id     bigint NOT NULL,
    op          text NOT NULL CHECK (op IN ('add', 'update', 'delete', 'no-op')),
    old_val     jsonb,
    new_val     jsonb,
    at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX memory_audit_memory_idx ON memory_audit (memory_id);
CREATE INDEX memory_audit_user_idx   ON memory_audit (user_id);

-- ── 사용자 격리 (RLS) ──────────────────────────────────────────────
-- 세션에서 SET LOCAL app.current_user_id = '<id>' 후 접속 사용자만 자기 행 접근.
-- 미설정 시 NULL → 0행(deny-by-default). FORCE 로 소유자도 정책 적용.
-- 앱은 반드시 비-superuser 역할(skinmate_app, 004)로 접속해야 RLS 적용됨.
ALTER TABLE memories     ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories     FORCE  ROW LEVEL SECURITY;
ALTER TABLE memory_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_audit FORCE  ROW LEVEL SECURITY;

CREATE POLICY memories_user_isolation ON memories
    FOR ALL
    USING      (user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
    WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint);

CREATE POLICY memory_audit_user_isolation ON memory_audit
    FOR ALL
    USING      (user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint)
    WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::bigint);

COMMIT;
