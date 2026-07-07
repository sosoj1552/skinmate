-- 004_app_role_and_grants.sql
-- 애플리케이션 접속 역할 + 권한. 담당 A (RLS 설계와 짝, ⭐ 리뷰).
-- 근거: PRD.md F1/F5(격리), consensus-plan R4.
--
-- 왜 별도 역할인가:
--   POSTGRES_USER(skinmate)는 superuser → RLS 를 BYPASS 한다. 격리(AC-M5)가 실제로 동작하려면
--   앱과 테스트가 **비-superuser, NOBYPASSRLS 역할**로 접속해야 한다. 그 역할이 skinmate_app.
--   앱은 접속 후 매 요청에서 `SET app.current_user_id = '<id>'` 로 스코프를 건다.
--
-- [주의] 아래 비밀번호는 로컬/CI 전용. 운영 전 반드시 교체(secret 관리).

BEGIN;

-- 역할 생성 (멱등)
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'skinmate_app') THEN
        CREATE ROLE skinmate_app LOGIN PASSWORD 'skinmate-app-dev-only'
            NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END $$;

-- 관계형 스키마 권한
GRANT USAGE ON SCHEMA public TO skinmate_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO skinmate_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO skinmate_app;
-- 이후 마이그레이션으로 생기는 객체에도 기본 권한 부여
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO skinmate_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO skinmate_app;

-- AGE 그래프 접근 권한.
--   그래프 데이터는 그래프명과 동일한 스키마('skinmate')에 저장된다.
--   choke.age_exec 가 cypher() 를 실행하려면 ag_catalog + 그래프 스키마 접근이 필요.
--   [리뷰 포인트] AGE 권한은 예민함 — 004_graph smoke(아래 apply/smoke)로 실검증 필수.
GRANT USAGE ON SCHEMA ag_catalog TO skinmate_app;
GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO skinmate_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA ag_catalog TO skinmate_app;

GRANT USAGE ON SCHEMA skinmate TO skinmate_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA skinmate TO skinmate_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA skinmate TO skinmate_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA skinmate
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO skinmate_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA skinmate
    GRANT USAGE, SELECT ON SEQUENCES TO skinmate_app;

COMMIT;
