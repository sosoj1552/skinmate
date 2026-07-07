# 데이터 모델 — skinmate (단일 진실원)

> 확정일: 2026-07-07 (인터뷰·합의 이후 2인 재설계). **이 문서가 스키마의 단일 진실원.**
> skinmate-consensus-plan.md §4의 초안을 대체한다. 마이그레이션은 `db/migrations/`.
> 설계 원칙: **테이블은 (1) 하드 안전장치가 필요하거나 (2) 개인 사실의 진실원일 때만 둔다. 순수 지식 관계는 그래프 네이티브로.**

---

## 1. 관계형 테이블 (6개)

| 테이블 | 용도 | 진실원 | 임베딩/그래프 연결 |
|---|---|---|---|
| **users** | 사용자 계정 + 피부타입 | ✅ 사용자 | → `User` 노드 |
| **ingredients** | 성분 정보(성분명 ko/en·효과·분류·등급·성분소개·canonical_key) | ✅ 성분 | → `Ingredient` 노드 |
| **products** | 제품 정보(제품명·브랜드·설명) + **설명 embedding(1024)** | ✅ 제품 | → `Product` 노드 / **벡터** |
| **product_ingredients** | 제품↔성분 junction(FK) | ✅ containment | → `CONTAINS` 엣지 / **하드필터** |
| **documents** | 성분·제품·피부관리 프로즈 아티클 + **embedding(1024)** | ✅ 문서 | **벡터** + 그래프 지식 추출 원천 |
| **memories** | 개인 맥락(회피/선호·고민·피부타입·**계절**) + 가중치, RLS | ✅ 개인 사실 | → 개인 엣지(AVOIDS/PREFERS/HAS_CONCERN) |
| **memory_audit** | 기억 CRUD 감사(add/update/delete/no-op, old→new), RLS | — 감사 | 없음 |

> 표에는 memory_audit 포함 7행이지만 "엔티티/관계 테이블"은 6개 + 감사 1개.

### 삭제된 테이블 (그리고 그 내용이 간 곳)
| 삭제 | 대체 |
|---|---|
| concerns | memories `fact_type='has_concern'` + `target_name`; 그래프 `Concern` 노드(name 키) |
| seasons · season_concerns | memories `season`(text) 개인 맥락 |
| conversations | **서버 stateless** — 클라이언트가 히스토리 전달, 퍼널 상태는 매 턴 재계산 |
| data_sources | 각 행의 `source_meta`(jsonb) — url·kind·crawled_at·robots_ok |
| ingredient_concerns · ingredient_relations | **그래프 네이티브** TREATS/HELPS (아래 §2) |

### memories 컬럼 (다리·격리 핵심)
`memory_id, user_id, content, fact_type, slot_key, season, base_weight, frequency, last_seen, target_ingredient_id(FK), target_name, created_at, deleted_at`
- `fact_type` enum: `skin_type·avoid_ingredient·prefer_ingredient·avoid_brand·prefer_brand·has_concern·other`.
- 기억→그래프 다리: ingredient는 `target_ingredient_id`(FK), concern/brand는 `target_name`(그래프 네이티브 노드 키).
- `effective_weight = base_weight × exp(-λ × Δdays)`, **λ=0.05/day**. 조회 시 계산(저장 안 함).

---

## 2. 그래프 (AGE `skinmate`)

- **노드(5)**: `User · Ingredient · Product · Concern · Brand`  *(Season·Formulation 노드 없음)*
- **전역 엣지**:
  - `(:Product)-[:CONTAINS]->(:Ingredient)` ← **product_ingredients 투영**(관계형 진실원)
  - `(:Ingredient)-[:TREATS|AGGRAVATES]->(:Concern)` ← **그래프 네이티브**(인제스트가 문서·크롤에서 추출·적재)
  - `(:Ingredient)-[:HELPS|CONFLICTS]->(:Ingredient)` ← **그래프 네이티브**
- **개인 엣지**(memories 투영, `user_scope` 프로퍼티):
  - `(:User)-[:HAS_CONCERN {season?}]->(:Concern)`
  - `(:User)-[:AVOIDS|PREFERS]->(:Ingredient|Brand)`

### 두 종류의 엣지 원천 (헷갈리지 말 것)
| 엣지 | 원천 | 이유 |
|---|---|---|
| CONTAINS | 관계형 product_ingredients | 하드필터(AC-R2) 안전장치를 관계형에 둠 |
| AVOIDS/PREFERS/HAS_CONCERN | 관계형 memories | 개인 사실 진실원(가중치·감사·RLS) |
| TREATS/AGGRAVATES·HELPS/CONFLICTS | **그래프 네이티브**(테이블 없음) | 순수 지식 관계 — 하드보장·개인진실원 아님 |

- **격리:** 관계형은 RLS. 그래프는 RLS 없음 → 모든 접근이 `choke.age_exec(user_scope,…)` 단일 관문. 개인 엣지에 `user_scope` 강제(AC-G3).
- 재구축: CONTAINS·개인 엣지는 관계형에서 재투영. TREATS/HELPS는 재-인제스트(크롤 캐시로 idempotent).

---

## 3. 벡터 (pgvector)

- `products.embedding`, `documents.embedding` = **vector(1024)** (bge-m3), HNSW cosine.
- 모든 벡터 행에 `embedding_model_id` (재-임베딩 마이그레이션 대비).
- `memories`에는 벡터 컬럼 없음 — 개인 사실 dedup은 쓰기 시 그 사용자 소량 사실에 대해 즉석 임베딩 비교.
- 제형(에멀전/오일/젤/끈적임)은 **products.embedding으로만** 근사(구조화 노드·태그 없음). 제형 soft-ranking(AC-F1).

---

## 4. 확정된 설계 결정 요약

| # | 결정 | 근거 |
|---|---|---|
| 하드필터 | 회피성분 0건은 관계형 product_ingredients(FK junction)로 ACID 보장 | 그래프 정확성과 무관한 안전장치(AC-R2) |
| 지식 엣지 | TREATS/HELPS는 테이블 없이 그래프 네이티브 | 순수 순회용 — 테이블은 순수 중복 |
| 계절 | 글로벌 큐레이션 대신 memories.season 개인 맥락 | 사람마다 다르고 깨끗한 소스 없음 |
| 고민 | 캐논 테이블 대신 기억 카테고리 + 그래프 노드 | 대화에서 정의·기억 |
| 대화 | conversations 없음(stateless) | v1 프로토타입엔 충분, 핵심은 memories에 보존 |
| 격리 | skinmate_app + `SET LOCAL app.current_user_id` GUC + RLS FORCE | superuser BYPASS 방지, 풀 누수 방지 |
