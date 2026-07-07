#!/usr/bin/env bash
# 스키마 실검증: 테이블 존재 · RLS 격리(AC-M5 미니) · AGE 라벨 · skinmate_app cypher 실행.
# 전제: apply-migrations.sh 완료.
set -euo pipefail

DB_USER="${POSTGRES_USER:-skinmate}"
DB_NAME="${POSTGRES_DB:-skinmate}"
APP_PW="skinmate-app-dev-only"   # 004 마이그레이션과 일치(로컬/CI 전용)

# superuser psql
SU=(docker compose exec -T db psql -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 -tA)
# app-role psql (RLS 적용 대상)
APP=(docker compose exec -T -e PGPASSWORD="$APP_PW" db psql -U skinmate_app -d "$DB_NAME" -v ON_ERROR_STOP=1 -tA)

# app 역할로 특정 사용자 스코프에서 SQL 실행.
# GUC(app.current_user_id)를 PGOPTIONS로 연결 시작 시 주입 → SET/SELECT 혼선 없이 RLS 적용.
app_scoped() {  # $1=user_id, $2=sql
  docker compose exec -T -e PGPASSWORD="$APP_PW" -e PGOPTIONS="-c app.current_user_id=$1" \
    db psql -U skinmate_app -d "$DB_NAME" -v ON_ERROR_STOP=1 -tA -c "$2"
}

echo "[1/5] core tables present"
cnt=$("${SU[@]}" -c "SELECT count(*) FROM information_schema.tables
                     WHERE table_schema='public' AND table_name IN
                     ('users','ingredients','products','product_ingredients',
                      'documents','memories','memory_audit');")
[ "$cnt" = "7" ] || { echo "ERROR: expected 7 core tables, found $cnt"; exit 1; }

echo "[2/5] seed 2 users (superuser)"
"${SU[@]}" -c "INSERT INTO users(skin_type) VALUES ('oily'),('dry');" >/dev/null
u1=$("${SU[@]}" -c "SELECT min(user_id) FROM users;")
u2=$("${SU[@]}" -c "SELECT max(user_id) FROM users;")
[ "$u1" != "$u2" ] || { echo "ERROR: need 2 distinct users"; exit 1; }

echo "[3/5] RLS isolation as skinmate_app"
# u1 스코프로 기억 1건 삽입
app_scoped "$u1" "INSERT INTO memories(user_id,content,fact_type) VALUES ($u1,'레티놀 안 맞음','avoid_ingredient');" >/dev/null
# u1 은 자기 기억 보임(>=1)
own=$(app_scoped "$u1" "SELECT count(*) FROM memories;")
[ "$own" -ge 1 ] || { echo "ERROR: u1 should see own memory, saw $own"; exit 1; }
# u2 스코프에서 u1 기억 안 보임(0) — 크로스유저 누수 0건
cross=$(app_scoped "$u2" "SELECT count(*) FROM memories;")
[ "$cross" = "0" ] || { echo "ERROR: cross-user leak! u2 saw $cross rows"; exit 1; }

echo "[4/5] AGE graph + labels present"
labels=$("${SU[@]}" -c "SELECT count(*) FROM ag_catalog.ag_label l
                        JOIN ag_catalog.ag_graph g ON g.graphid=l.graph
                        WHERE g.name='skinmate' AND l.name IN
                        ('User','Ingredient','Product','Concern','Brand',
                         'CONTAINS','TREATS','AGGRAVATES','HELPS',
                         'CONFLICTS','HAS_CONCERN','AVOIDS','PREFERS');")
[ "$labels" = "13" ] || { echo "ERROR: expected 13 graph labels, found $labels"; exit 1; }

echo "[5/5] skinmate_app can run cypher via AGE"
# age 는 shared_preload_libraries 로 이미 로드됨 → 비-superuser 앱 역할은 LOAD 'age' 불필요/금지.
"${APP[@]}" -c "SET search_path=ag_catalog,public;
                SELECT * FROM cypher('skinmate', \$\$ RETURN 1 \$\$) AS (n agtype);" >/dev/null

echo "OK: schema smoke passed (tables · RLS isolation · graph labels · app cypher)"
