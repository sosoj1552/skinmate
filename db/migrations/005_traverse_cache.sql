-- 005_traverse_cache.sql
-- WBS 1A.9 순회 캐시 레이어 도입을 위한 캐시 테이블 및 무효화 트리거 생성

BEGIN;
SET search_path = public;

-- 1. 캐시 테이블 생성
CREATE TABLE IF NOT EXISTS public.traverse_cache (
    user_id INT NOT NULL,
    season VARCHAR(50) NOT NULL,
    paths_json JSONB NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, season)
);

-- 2. RLS(Row Level Security) 활성화 및 강제 적용(FORCE)
-- FORCE ROW LEVEL SECURITY를 적용해야 테이블 소유자나 Superuser 접속 세션에서도 RLS 정책이 작동합니다.
ALTER TABLE public.traverse_cache ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.traverse_cache FORCE  ROW LEVEL SECURITY;

-- 3. RLS 격리 정책 생성 (현재 세션 유저 격리)
DROP POLICY IF EXISTS traverse_cache_isolation_policy ON public.traverse_cache;
CREATE POLICY traverse_cache_isolation_policy ON public.traverse_cache
    FOR ALL
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer)
    WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::integer);

-- 4. skinmate_app 역할에 권한 부여
GRANT SELECT, INSERT, UPDATE, DELETE ON public.traverse_cache TO skinmate_app;

-- 5. memories 변경 감지 시 캐시 무효화 함수 정의
CREATE OR REPLACE FUNCTION public.fn_invalidate_traverse_cache()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        DELETE FROM public.traverse_cache WHERE user_id = OLD.user_id;
    ELSE
        DELETE FROM public.traverse_cache WHERE user_id = NEW.user_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = public;

-- 6. memories 테이블에 트리거 결합
DROP TRIGGER IF EXISTS trg_invalidate_traverse_cache ON public.memories;
CREATE TRIGGER trg_invalidate_traverse_cache
AFTER INSERT OR UPDATE OR DELETE ON public.memories
FOR EACH ROW
EXECUTE FUNCTION public.fn_invalidate_traverse_cache();

COMMIT;
