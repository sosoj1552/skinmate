# 기술 스택 & 프로젝트 환경 (실행용)

> 대상 독자: 이 프로젝트를 구현할 LLM/개발자. 착수 전 반드시 읽는다.
> 근거: [team-agreement.md](../team-agreement.md), [skinmate-consensus-plan.md](../skinmate-consensus-plan.md)

---

## 1. 기술 스택 (고정)

| 영역 | 선택 | 버전/비고 |
|------|------|-----------|
| 언어 | Python | **3.12** |
| DB | PostgreSQL | **16** (단일 인스턴스) |
| 그래프 | Apache AGE | PG16 호환 태그로 **정확히 고정** (`shared_preload_libraries=age`) |
| 벡터 | pgvector | PG16 호환 태그로 **정확히 고정** |
| DB 드라이버 | psycopg | **3.x** (`psycopg[binary]`) — 명시적 트랜잭션 제어 |
| 임베딩 모델 | BAAI/bge-m3 | **1024-dim**, 다국어(KO+EN). 처음엔 로컬 실행(`sentence-transformers`/`FlagEmbedding`), 이후 컨테이너 |
| LLM | Claude API | `llm/` 추상화 뒤. 모델 ID는 `claude-sonnet-5` 기본(추론 무거우면 `claude-opus-4-8`) |
| API 서버 | FastAPI + uvicorn | `/chat`, `/admin` |
| 크롤 | httpx (+ 필요시 scrapy) | rate-limit·캐시 |
| 테스트 | pytest, pytest-cov | 상세는 [ACCEPTANCE-TESTING.md](ACCEPTANCE-TESTING.md) |
| 린트/포맷 | ruff + black | line length 100 |
| 타입 | mypy | `strict` 지향(점진 적용) |
| 마이그레이션 | 순번 SQL 파일 | `db/migrations/001_*.sql` (프레임워크 미사용) |

**되돌리기 어려운 확정 상수** (변경 시 재-임베딩/재-마이그레이션 필요, 상대 합의 필수):
`D_DOC=1024`, `embedding_model_id='bge-m3'`, `λ=0.05/day`, 그래프 이름 `skinmate`, 성분임베딩(D_ING) 미사용.

---

## 2. 개발 환경 셋업

### 2.1 전제
- Docker Desktop (Windows 10), Python 3.12, Git.
- Claude API 키(환경변수 `ANTHROPIC_API_KEY`).

### 2.2 순서
```bash
# 1) DB 기동 (Postgres16 + AGE + pgvector 확장 포함 이미지)
docker compose up -d db

# 2) Python 가상환경
python -m venv .venv
. .venv/Scripts/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"        # pyproject.toml의 의존성

# 3) 마이그레이션 적용 (순번 SQL 실행 스크립트)
python -m scripts.migrate up

# 4) 로컬 임베딩 모델 최초 다운로드(~2GB) 워밍업
python -m scripts.warm_embedder

# 5) 공용 샘플 시드
python -m scripts.seed --fixtures eval/fixtures

# 6) 앱 기동
uvicorn skinmate.app.main:app --reload
```

### 2.3 환경변수 (`.env`, 커밋 금지)
| 변수                  | 용도                                                  |
| ------------------- | --------------------------------------------------- |
| `ANTHROPIC_API_KEY` | Claude API                                          |
| `DATABASE_URL`      | `postgresql://skinmate:...@localhost:5432/skinmate` |
| `EMBEDDER_MODE`     | `local`(기본) / `container` / `api` — ⭐9d 스왑 스위치      |
| `EMBEDDER_ENDPOINT` | container/api 모드일 때 URL                             |
| `CRAWL_RATE_LIMIT`  | 초당 요청 수(기본 1.5)                                     |

---

## 3. 디렉토리 레이아웃 & 소유

```
skinmate/
├── db/
│   ├── initdb/                 # CREATE EXTENSION vector, age; roles      [A]
│   └── migrations/             # 001_*.sql 순번                            [A ⭐]
├── src/skinmate/
│   ├── knowledge/              # 성분·제품·관계 + 하드필터                  [A]
│   ├── documents/              # embed.py(⭐), search.py                    [A]
│   ├── graph/                  # ontology, projection, choke.py(⭐), traverse [A]
│   ├── retrieval/              # retrieve.py 융합(⭐6)                       [A]
│   ├── memory/                 # crud, weight, repository                   [B]
│   ├── write/                  # writer.py 원자 저장(⭐4)                    [B]
│   ├── chat/                   # orchestrator, rationale                    [B]
│   ├── llm/                    # Claude 추상화                              [B]
│   ├── contracts/              # 공유 데이터 형식(⭐7)                       [공동]
│   └── app/                    # FastAPI                                    [B]
├── ingest/                     # sources/, normalize.py                     [A]
├── eval/                       # 테스트 하네스 + fixtures/                   [B]
├── scripts/                    # migrate, seed, warm_embedder, benchmark    [공동]
└── docs/                       # 본 문서 세트
```

**규칙:** 자기 소유 폴더는 자유. ⭐ 파일(특히 `db/migrations/`, `contracts/`)은 PR + 상대 리뷰 필수.

---

## 4. 코딩 컨벤션

### 4.1 Python 일반
- 포맷: `black`(line 100), 린트: `ruff`, 타입: `mypy`. **PR 전 세 개 통과가 CI 게이트.**
- 네이밍: 모듈/함수 `snake_case`, 클래스 `PascalCase`, 상수 `UPPER_SNAKE`.
- **모든 공개 함수에 타입 힌트 + 한 줄 docstring**(무엇을/왜).
- 예외는 도메인 예외(`skinmate.errors`)로 감싸 던진다. 광범위 `except Exception`은 로깅+재던짐만.
- 로깅은 `structlog`(구조화). print 금지.

### 4.2 DB / 트랜잭션 (⭐ 불변식 — 위반 시 PR 리젝)
- **저장 경로에 autocommit 금지.** 원자 저장은 **한 커넥션에서** `BEGIN → … → COMMIT`, 예외 시 `ROLLBACK`. → [PRD.md](PRD.md) F5.
- **AGE 접근은 오직 `choke.age_exec(...)` 통로로만.** 앱 코드에서 raw `cypher()` 직접 호출 금지. **CI에 grep 가드**(`rg "cypher\("` 가 choke.py 외부에서 매칭되면 실패).
- **앱 역할(`skinmate_app`)은 `LOAD 'age'` 호출 금지.** age는 `shared_preload_libraries`로 이미 로드됨 → 비-superuser의 LOAD는 거부된다(스모크 검증). choke는 `SET search_path = ag_catalog, …` 로 세팅하거나 `ag_catalog.cypher(...)` 로 자격 호출만.
- **무자격 DDL은 `public`에.** DB 기본 search_path가 `"$user", public, ag_catalog` 라 무자격 `CREATE TABLE`은 public에 생성됨(ag_catalog 오염 방지). 마이그레이션도 `SET LOCAL search_path = public` 안전벨트.
- **개인 데이터 접근엔 항상 `user_id`/`user_scope`.** memories 등 관계형은 RLS 정책, AGE는 choke가 scope 주입.
- **벡터를 쓰는 모든 행에 `embedding_model_id` 기록.** 모델 스왑은 재-임베딩 마이그레이션으로만.
- 스키마 변경은 **새 번호 마이그레이션 파일**로만. 기존 파일 수정 금지.

### 4.3 성분 정규화
- 성분 canonical key = **INCI 명**(있으면), 없으면 한글명 정규화. 중복은 canonical key로 병합.
- 이름→ID 해석 실패 시: 그래프 엣지는 **skip + WARN 로그**(파이프라인 중단 금지). → [PRD.md](PRD.md) F4.

### 4.4 Git / 브랜치
- `main` + 도메인별 feature 브랜치(`feat/A-ingest-coos`, `feat/B-writer` 등).
- 커밋 메시지: `<scope>: <요약>` (한글 가능). 예: `writer: 단일 트랜잭션 원자 저장 구현`.
- **커밋·푸시 전 사용자 확인**(프로젝트 규칙).
- ⭐ 파일 PR은 상대 리뷰어 승인 없이 머지 금지.

### 4.5 LLM 호출
- 프롬프트는 `llm/prompts/`에 파일로 분리(코드에 인라인 금지) → 테스트에서 재현·고정.
- fact 추출·CRUD 판정 등 LLM 출력은 **JSON 스키마 강제**(구조화 출력) + 파싱 실패 시 재시도 1회 후 no-op 폴백.
- 테스트에서는 실제 API 대신 **녹화 응답(fixture)** 사용(비용·재현성). → [ACCEPTANCE-TESTING.md](ACCEPTANCE-TESTING.md).
