# db/migrations

순번 SQL 마이그레이션. **프레임워크 미사용**(consensus-plan 결정). 파일명 `NNN_<설명>.sql`, 번호 오름차순 1회 적용.

## 규칙
- **freeze 후 기존 파일 수정 금지.** 스키마 변경은 **새 번호 파일**로만 추가하고 상대 트랙에 공지.
- ⭐ 파일(스키마 전반)은 PR + 상대 리뷰 필수.
- `db/initdb/00-extensions.sql`(확장 생성)은 최초 컨테이너 기동 시 1회 자동 실행. 마이그레이션은 그 **이후** 별도 적용.

## 파일
| 번호 | 파일 | 담당 | 내용 |
|---|---|---|---|
| 001 | `001_core_knowledge.sql` | A | users·ingredients·products·product_ingredients·documents + 벡터 인덱스 |
| 002 | `002_memory_and_rls.sql` | B(초안)→A(통합) | fact_type enum·memories·memory_audit + RLS(격리) |
| 003 | `003_graph_ontology.sql` | A | AGE 그래프 `skinmate` + 노드/엣지 라벨(멱등) |
| 004 | `004_app_role_and_grants.sql` | A | `skinmate_app` 역할(비-superuser, NOBYPASSRLS) + 권한 |

전체 데이터 모델(테이블·그래프·벡터의 근거와 용도)은 [../../docs/DATA-MODEL.md](../../docs/DATA-MODEL.md)가 단일 진실원.

## 적용 & 검증 (임시 러너 — Python 러너는 WBS 0.1에서 대체)
```bash
docker compose up -d db          # DB 기동 (healthy 대기)
bash scripts/apply-migrations.sh # 001~004 순서대로 적용
bash scripts/schema-smoke.sh     # 스키마·RLS 격리·그래프 라벨 실검증
```

## 핵심 불변식 (스키마가 강제하는 것)
- **격리:** memories/memory_audit RLS + FORCE. 앱은 `skinmate_app`으로 접속 후 `SET LOCAL app.current_user_id`. superuser 접속 시 RLS BYPASS되므로 격리 테스트는 반드시 `skinmate_app`으로.
- **진실원:** product_ingredients(CONTAINS·하드필터), memories(개인 사실·계절·고민). CONTAINS·개인 엣지는 관계형에서 projection; TREATS/HELPS 등 지식 엣지는 그래프 네이티브(인제스트 적재).
- **임베딩:** D_DOC=1024, 행마다 embedding_model_id.
