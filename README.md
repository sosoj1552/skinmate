# SkinMate 🧴

기억·지식·관계그래프를 컨텍스트로 맞춤형 화장품(성분/제품)을 추천하는 대화형 AI 시스템

> **상태**: fresh-start 인터뷰 완료(ambiguity 16.5% PASSED) → 합의 플랜 pending approval. 구현 착수 전. 실행은 별도 승인 필요.
>
> ℹ️ 이 문서/설계는 **새 인터뷰 기반 재설계**입니다. 이전 async 설계 계획은 폐기되었습니다.

## 프로젝트 개요

**목표**: 로그인한 여러 사용자가 각자 개인화된 대화형 화장품 추천을 받는다. 시스템은 세 컨텍스트 소스를 결합한다.

1. **구조화 지식(Structured Knowledge)** — 성분(등급·효과·분류)·제품·성분-제품 관계를 관계형 테이블로 저장 (전역 공유)
2. **문서 임베딩(Documents/RAG)** — 피부관리 방식 등 프로즈 문서를 pgvector 임베딩으로 유사도 검색 (전역 공유)
3. **기억(Memory)** — LLM이 중요 사실만 선별 관리, 빈도·최근성 가중치로 우선순위화 (사용자별 격리)
4. **관계 그래프(Graph)** — Apache AGE로 관계 저장, **2-hop 이상 다단계 순회가 추천의 핵심 추론** (전역 지식 + 사용자별 격리)

추천 산출물은 **적응형**이다 — 질문이 구체적(제형·보습 명시)이면 바로 제품명+근거, 모호하면 조언에서 시작해 대화로 좁혀 최종 제품에 도달한다.

## 기술 스택

- **DB**: 단일 PostgreSQL 인스턴스 + `age`(그래프) + `vector`(pgvector 임베딩) 확장 공존
- **저장 원자성**: 관계형·그래프·벡터가 한 Postgres 안에 있어 **단일 트랜잭션으로 원자적 쓰기**(하나 실패 시 전체 롤백) — 분산 saga 불필요
- **기억 쓰기**: **동기(synchronous)** — 응답 생성 후 같은 요청에서 원자적 트랜잭션으로 기록. 큐·워커·drain 없음
- **LLM**: 프로바이더 추상화 (기본 Claude API, 교체 가능)
- **임베딩**: BAAI/bge-m3 (1024-dim, KO+EN), 문서/제품 임베딩 공간. 벡터 행마다 `embedding_model_id` 저장(재임베딩 마이그레이션 대비)
- **구현 언어(권장)**: Python (`psycopg` + AGE/pgvector, `httpx`/`scrapy` 크롤)

## 핵심 설계 결정

### 기억 관리 (Memory CRUD)
LLM이 중요 사실 여부를 판단해:
- **신규 사실 → add**
- **값 변경 → update** (같은 슬롯, 새 값) — 예: `피부타입 건성 → 복합성`
- **철회/무효 → delete** (대체값 없이 슬롯 소멸) — 예: "임신 중 레티놀 회피" → "출산함"
- **중복 → no-op**
- 일상·비도메인 정보("오늘 피곤해")는 미저장. delete는 soft-delete + 감사로그로 오삭제 방지

### 가중치 (사람다운 중요도)
```
effective_weight = base_weight × exp(-λ × days_since_last_seen)      (λ = 0.05/day, ≈14일 half-life)
```
자주·최근 언급될수록 검색 상위. `base_weight`는 언급 빈도로 증가.

### 저장 동기화 (동기 원자적)
세 저장 영역(관계형·pgvector·AGE)이 한 Postgres에 있으므로, 워커는 **한 커넥션·한 트랜잭션**으로 씀:
```
BEGIN → 관계형 INSERT/UPDATE + pgvector 쓰기 + AGE cypher() 쓰기 → COMMIT   (예외 시 ROLLBACK)
```
큐/워커/drain/dead-letter 없음. 기억 쓰기는 응답 생성 **후** 실행되므로 답변 품질엔 영향 없고, 커밋이 턴 종료 전에 끝나 다음 턴은 항상 최신 기억을 봄(교차턴 staleness 소멸). 대가는 턴당 ~1–3s 지연 — 나중에 `write/writer.py` 뒤에서 async로 승격 가능.

### 관계 그래프 (파생 projection)
- **관계형 테이블이 진실원**, AGE 엣지는 결정적 projection 잡으로 재빌드(수기 작성 금지)
- **전역 엣지**: `(:Season)-[:AFFECTS]->(:Concern)` (seasons/season_concerns에서), `CONTAINS`(product_ingredients에서), `TREATS/AGGRAVATES`, `HELPS/CONFLICTS`
- **기억→그래프 브릿지**: `fact_type='avoid_ingredient'` 기억 행 → 사용자 스코프 `(:User)-[:AVOIDS]->(:Ingredient)` 엣지로 projection (같은 원자 트랜잭션 내, choke-point 경유)
- **제형(제형)은 그래프 노드가 아님** — 제품 설명 임베딩 유사도로만 근사(best-effort). 하드 0건 보장은 **성분만**

### 사용자 격리 (하드 불변식)
- `memories` 등 관계형 → PostgreSQL RLS(`user_id`)
- AGE 서브그래프 → RLS 미적용이므로 **choke-point 함수**가 `user_scope` 강제 + 크로스유저 누수 0건 CI 테스트

## 데이터 수집

- **크롤 소스(확정)**: [coos.kr](https://coos.kr/) (성분 canonical 키 + 한글명/등급) + [Paula's Choice Beautypedia](https://www.paulaschoice.co.kr/about-beautypedia) (성분 해설 문서 + 제품 제형 서술)
- robots.txt 없음 → 정중한 크롤(rate-limit ~1–2 req/s, 캐시, User-Agent 명시, 출처·수집일시 저장). *ToS/저작권은 유효 → 비상업 전제, 상업화 시 재검토*
- `season_concerns`는 수동 시드(~10–20줄). 제형/텍스처 토큰 ≥60% 데이터 적합성 게이트 통과 확인, 미달 시 수동 보강

## 대표 시나리오

> "가을이 되니 건조하다.. 끈적한 제형은 싫어서 오일은 아니었으면 좋겠어. 에멀전 제형인데 보습은 확실한 제품으로 추천해줄래?"

1. 과거 고민·선호 회상(기억) + 현재 질문 이해
2. 검색: 계절→문제(가을→건조) 순회 + 회피 성분 하드 제외 + 제형 임베딩 랭킹(에멀전>오일) + 보습 성분/제품
3. 근거 기반 답변 — 순회 경로와 회상된 기억을 근거로 제시

## 수용 기준 (요약)

| 그룹 | 핵심 |
|------|------|
| Documents/Knowledge | 실데이터 적재·유사도 검색(AC-D1), 성분→제품 조회(AC-D2) |
| Memory | CRUD 정확(AC-M1), weight 순위(AC-M2), 중요도 분류(AC-M3), 기억 반영도(AC-M4), 격리 0건(AC-M5) |
| Storage Sync | 3-store 원자성·부분적용 0(AC-S1), 동기 가시성(AC-S2) |
| Graph | Cypher 순회(AC-G1), 2+ hop 근거 도출(AC-G2), 서브그래프 격리 0건(AC-G3) |
| Chat/Rec | 적응형 산출물(AC-R1), 회피성분 0건 하드(AC-R2), 근거 정합(AC-R3), 대표 시나리오 완주(AC-R4), 제형 랭킹(AC-F1) |

## 문서

- 📋 [요구사항 스펙 (Deep Interview)](deep-interview-skinmate.md) — 토폴로지, 목표, 제약, 수용 기준, 온톨로지, 인터뷰 트랜스크립트
- 📐 [합의 플랜 (Consensus Plan)](skinmate-consensus-plan.md) — 아키텍처, 데이터 모델, 구현 단계, 리스크, ADR (Architect + Critic 검토 반영)
- ❓ [미결 사항 (Open Questions)](open-questions.md)
- 🤝 [팀 합의본 (Team Agreement)](team-agreement.md) — 2-worker 역할 분담, ★ 공유 seam 확정본, 협업 규칙

### 실행 스펙 (착수하는 LLM/개발자용, `docs/`)

- 🧱 [데이터 모델 (Data Model)](docs/DATA-MODEL.md) — **스키마 단일 진실원**: 관계형 6 + 그래프(AGE) + 벡터, 설계 결정 근거
- 🗂️ [WBS & TODO](docs/WBS.md) — 작업 분해, 우선순위(P0/P1/P2), 의존 그래프, 임계경로, 핵심 우선 작업
- 📄 [PRD (기능 상세)](docs/PRD.md) — 기능별 목표·사용자 시나리오·비즈니스 로직·예외 처리
- 🧰 [환경 & 컨벤션](docs/ENVIRONMENT.md) — 기술 스택, 개발환경 셋업, 디렉토리 소유, 코딩 컨벤션
- ✅ [인수조건 & 테스트](docs/ACCEPTANCE-TESTING.md) — AC 완료 체크리스트, 테스트 계층·도구, 핵심 테스트, CI 게이트

## 착수 전 남은 확인

1. Paula's Choice 제품 페이지가 AC-D1 제형 토큰 ≥60% 게이트를 실제로 통과하는지 적재 시 실측 (미달 시 수동 보강)
2. AGE 2-hop 순회 p95 벤치마크 (지연 예산 초과 시에만 read-through 캐시 폴백)

---
**Created**: 2026-07-06 · **Updated**: 2026-07-06
**다음 단계**: 실행(team/ralph/autopilot)은 별도 승인 필요 — 현재 자동 시작 안 함
