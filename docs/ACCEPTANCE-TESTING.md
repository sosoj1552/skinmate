# 인수조건 & 테스트 계획 (실행용) — skinmate

> 대상 독자: 구현 LLM/개발자. "이 기능이 어떻게 동작하면 완료인가"의 체크리스트 + 무엇을 어떤 도구로 테스트하는가.
> 근거: [skinmate-consensus-plan.md](../skinmate-consensus-plan.md) §6/§8, [PRD.md](PRD.md).

---

## 1. 완료 기준 체크리스트 (AC별, 조작 가능한 임계치)

각 AC는 **자동 테스트로 통과/실패가 판정**되어야 한다. WBS 작업 → AC 매핑은 [WBS.md](WBS.md) 참조.

### 데이터/문서 (담당 A)
- [ ] **AC-D1** 실데이터 적재 + 유사도 검색 + **제형 게이트**: ≥1 실소스 적재; top-k(k=5) 유사도 결과 비어있지 않고 출처 표기됨; **제형 토큰(에멀전/오일/끈적임/젤…)이 제품 설명의 ≥60%에 존재**.
- [ ] **AC-D2** 성분→제품: 알려진 성분이 `product_ingredients` 기준 정확한 제품 집합을 반환.

### 기억 (담당 B)
- [ ] **AC-M1** add/update/delete/no-op + **비파괴 공존**: 4 케이스 각각 올바른 op·상태; **추가**로 충돌 없는 새 언급 시 기존 참 사실이 삭제·덮어써지지 않고 공존.
- [ ] **AC-M2** 최근·빈도 상위: 고정 λ=0.05/day에서 seeded fixture의 `effective_weight` 순서와 랭킹 일치.
- [ ] **AC-M3** 중요도 분류: 라벨셋(≥20 발화, 잡담 vs 도메인)에서 **저장 precision ≥ 0.85, 도메인 recall ≥ 0.85**.
- [ ] **AC-M4** 기억 반영: 같은 질의에서 기억 有 vs 無 사용자의 추천이 **실질적으로 다름**(제품 ≥1개 상이 AND 근거 상이).
- [ ] **AC-M5** 격리: 크로스유저 기억 조회 **정확히 0행**.
- [ ] **AC-M6** 최근성 루프: 턴 N에서 쓴 사실 F가 (동기 커밋 후) 턴 N+1 조회에서 동급 base_weight 미언급 사실 대비 **랭크 상승**.

### 저장 동기화 (담당 B)
- [ ] **AC-S1** 원자 단일-tx: **AGE write 뒤 COMMIT 전** 결함 주입 → 3저장 **부분행 0**(먼저 쓴 관계형 행도 롤백, AGE·벡터 카운트 불변). 범위=턴당 기억 쓰기.
- [ ] **AC-S2** 동기 가시성: 기억 write가 **턴 반환 전 커밋**; 턴 N의 사실이 턴 N+1에서 drain 없이 즉시 보임.

### 제형 (담당 A/B)
- [ ] **AC-F1** 제형 soft-ranking: 에멀전 선호+오일 회피 사용자에 대해, 고정 고민 질의·제형 라벨 fixture에서 **에멀전 서술 제품이 동급 오일 서술 제품보다 상위(쌍 비교 ≥80%)**. (하드 아님)

### 그래프 (담당 A)
- [ ] **AC-G1** Cypher 순회: 시드 관계(성분→고민 TREATS 포함)가 choke 함수 통해 조회됨.
- [ ] **AC-G2** 2+hop 추천 + 경로 노출: ≥2hop 경로(개인 AVOIDS 엣지 포함)로 추천 도출 AND 경로가 근거에 방출됨.
- [ ] **AC-G3** 서브그래프 격리: 크로스유저 순회 **0행**.

### 대화/추천 (담당 B)
- [ ] **AC-R1** 적응형: 구체 질의 → 즉시 제품+근거; 모호 질의 → 조언 후 ≥1회 좁히기로 최종 제품 도달.
- [ ] **AC-R2** 회피 성분 하드: 무작위 추천 스위트에서 회피 성분이 추천 제품에 **0건**(제형은 best-effort).
- [ ] **AC-R3** 근거 정합: 모든 근거 주장이 실제 그래프 경로/기억에 매핑; 감사 표본에서 **미지원 주장 0건**.
- [ ] **AC-R4** 대표 시나리오: [PRD.md](PRD.md) §8의 4개 단언 (a~d) 전부 통과.

### 성능 예산
- [ ] 검색(비-LLM) p95 ≤ 400ms, 종단 p95 ≤ 2.5s (시드 기준). AGE 2-hop p95 벤치마크(R2, 초과 시에만 캐시 폴백).

---

## 2. 테스트 계층 & 도구

| 계층 | 도구 | 대상 |
|------|------|------|
| 유닛 | `pytest`, `pytest-cov` | 순수 로직: 가중치 공식, CRUD 판정 분기, INCI 정규화, 제형 토큰 카운터, 라우팅 규칙 |
| 통합(DB) | `pytest` + **docker-compose의 실제 PG16+AGE+pgvector** (전용 테스트 DB/스키마) | 마이그레이션, RLS, choke, writer 트랜잭션, projection, 벡터 검색 |
| 계약 | `pytest`(스키마 대조) — CI 게이트 | 스텁 fixture와 실물이 `RetrievalContext`·`IngredientRef`·`GraphPath` 스키마를 동일 만족 |
| E2E | `pytest` + FastAPI `TestClient` | 멀티턴 시나리오(AC-R1/R4), 최근성 루프(AC-M6) |
| LLM 의존 | **녹화 응답(fixture) 재생** + 소량 라이브 스모크 | fact 추출·CRUD 판정·근거 생성. 비용·재현성 위해 기본은 녹화 |
| 성능 | `pytest-benchmark` 또는 `scripts/benchmark.py` | 검색·순회 p95 |
| 정적 | `ruff`·`black --check`·`mypy` | 전 코드 (CI 게이트) |
| 보안 가드 | `rg` grep 가드 (CI) | choke 외부의 raw `cypher(` 호출 탐지 → 실패 |

---

## 3. 반드시 작성할 핵심 테스트 (놓치면 안 되는 것)

우선순위 P0 — 아키텍처 불변식을 지키는 테스트:

1. **격리 0행** (`test_isolation_cross_user.py`) — 사용자 2명 시드, A 컨텍스트로 B의 기억·그래프 조회 → **0행 단언**. RLS와 choke 양쪽. (AC-M5/G3)
2. **원자성 결함주입** (`test_writer_atomic_rollback.py`) — writer 트랜잭션에서 AGE write 직후 예외 주입 → 관계형/AGE/벡터 카운트 **불변** 단언(먼저 쓴 행도 롤백). (AC-S1)
3. **회피 성분 0건** (`test_hard_filter_zero.py`) — 회피 성분 등록 후 무작위 추천 스위트 → 추천 제품에 회피 성분 **0건**. (AC-R2)
4. **최근성 루프** (`test_recency_loop.py`) — 턴 N write → 턴 N+1 조회에서 랭크 상승, **drain 호출 없음**. (AC-M6/S2)
5. **2+hop 경로 근거** (`test_graph_2hop_rationale.py`) — 개인 AVOIDS 엣지 포함 ≥2hop 경로로 추천 + 경로가 근거 문자열에 포함. (AC-G2)

P1 — 기능 정확성:
6. CRUD 4케이스 + 비파괴 공존 (AC-M1)
7. 가중치 순위 (AC-M2), 중요도 분류 라벨셋 (AC-M3)
8. 제형 쌍 비교 ≥80% (AC-F1)
9. 대표 시나리오 4단언 (AC-R4)
10. 근거 정합 감사 표본 미지원 0건 (AC-R3)

---

## 4. 공유 Fixture (⭐8, `eval/fixtures/`)

- `seed_users.sql` — 격리 테스트용 사용자 2명(완전 분리 기억·그래프).
- `texture_labeled_products.json` — 제형 라벨(에멀전/오일/젤…) 붙은 제품 셋 → AC-F1 + AC-D1 보강 겸용.
- `weight_fixture.json` — 고정 `last_seen`/`frequency` → AC-M2 결정적 검증.
- `importance_labels.jsonl` — ≥20 발화(잡담 vs 도메인) → AC-M3.
- `llm_recordings/` — fact 추출·CRUD·근거 생성 녹화 응답.

**소유:** B가 관리, A가 D1/D2/F1용 실데이터 표본 제공. 스키마 변경 시 상대 합의.

---

## 5. CI 게이트 (PR 머지 조건)

1. `ruff` + `black --check` + `mypy` 통과.
2. 유닛 + 통합(실 DB) + 계약 테스트 통과.
3. **grep 가드**: choke 외부 raw `cypher(` 없음.
4. P0 핵심 테스트(§3의 1~5) 통과.
5. ⭐ 파일 변경 시 상대 리뷰어 승인.
6. 커버리지 기준선 유지(예: 신규 코드 라인 커버리지 ≥ 80%).

---

## 6. 개발-병행 원칙

테스트를 마지막에 몰지 않는다. 각 기능 작업(1A.*, 1B.*)은 **해당 유닛 테스트를 같은 PR에 포함**한다. 통합/E2E(AC-M4/R4/S1/M5)는 2단계에서 페어로. 이는 [WBS.md](WBS.md)의 1A.8·1B.7·2.1~2.6에 반영되어 있다.
