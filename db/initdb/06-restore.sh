#!/bin/bash
# 초기 기동 시 06번으로 실행되어 바이너리 덤프를 자동으로 pg_restore 복원합니다.
DUMP_FILE="/docker-entrypoint-initdb.d/skinmate_backup.dump"

if [ -f "$DUMP_FILE" ]; then
    echo "[initdb] Restoring PostgreSQL binary dump from $DUMP_FILE..."
    pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --no-owner "$DUMP_FILE"
else
    echo "[initdb] WARNING: Backup dump file not found at $DUMP_FILE. Skipping restore."
fi
