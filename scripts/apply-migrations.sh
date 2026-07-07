#!/usr/bin/env bash
# db/migrations/*.sql 를 번호 순서대로 적용 (임시 러너 — Python 러너는 WBS 0.1에서 대체).
# 전제: docker compose up -d db 로 db 컨테이너가 healthy.
# 적용은 superuser(POSTGRES_USER)로 수행 — DDL·역할 생성 권한 필요.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PSQL=(docker compose exec -T db psql -U "${POSTGRES_USER:-skinmate}" -d "${POSTGRES_DB:-skinmate}" -v ON_ERROR_STOP=1)

shopt -s nullglob
files=("$ROOT"/db/migrations/[0-9]*.sql)
if [ ${#files[@]} -eq 0 ]; then
  echo "ERROR: no migration files found"; exit 1
fi

for f in "${files[@]}"; do
  echo "[migrate] applying $(basename "$f")"
  "${PSQL[@]}" < "$f"
done

echo "OK: ${#files[@]} migration(s) applied"
