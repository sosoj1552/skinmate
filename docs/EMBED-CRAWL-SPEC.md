# 임베딩 상수 및 크롤링 설계 명세 (WBS 0.4)

본 문서는 SkinMate 프로젝트의 임베딩 상수 고정 정책 및 정중한 크롤링 행동 강령을 명세합니다. 이 사양은 향후 개발될 `embed.py`, 크롤러 모듈 및 `source_meta` 파싱 로직의 기준이 됩니다.

---

## 1. 임베딩 상수 고정 (D_DOC = 1024)

임베딩 모델 식별자와 벡터 출력 차원은 한번 물리적으로 DB에 저장되고 랭킹 로직에 사용되기 시작하면 사후 수정 비용이 매우 높으므로 아래 값으로 영구 고정합니다.

| 상수명 | 고정값 | 설명 |
|---|---|---|
| `D_DOC` | **1024** | BAAI/bge-m3 모델의 벡터 출력 차원수 |
| `embedding_model_id` | **'bge-m3'** | pgvector 컬럼에 벡터 저장 시 재-임베딩 대상을 식별하기 위해 함께 저장하는 모델 식별자 |

---

## 2. 정중한 크롤링(Crawl) 행동 강령

외부 화장품 성분 및 제품 설명을 기계적으로 수집할 때 상대 서버의 리소스를 존중하고 법적 리스크를 최소화하기 위해 아래 규범을 철저히 준수합니다.

### 2.1 수집 대상 및 범위
1. **coos.kr**:
   - 성분 표준 명칭, 한글명, 영문명, 등급, 배합 목적 정보 수집.
   - 성분 매핑의 canonical_key(INCI 기준) 획득용.
2. **Paula's Choice Beautypedia (paulaschoice.co.kr)**:
   - 성분별 피부 효능 해설 텍스트 및 등급 정보 수집.
   - 제품 상세 페이지 내 제형(Texture) 서술(에멀전/오일/젤 등) 획득용.

### 2.2 Rate-Limit 및 네트워크 예절
- **초당 요청 제어**: 초당 요청 수 `CRAWL_RATE_LIMIT`은 기본 **1.5회 이하**로 설정하며, 단일 스레드 기반으로 요청 간 최소 **0.7s ~ 1.0s의 딜레이**를 보장합니다.
- **로컬 캐싱 강제**: 수집 도중 오류가 발생해 재시작하더라도 중복 요청을 보내지 않도록, 다운로드한 HTML 응답은 로컬 `ingest/cache/` 디렉토리에 캐싱(SQLite DB 혹은 해시 파일)하여 활용합니다. 캐시의 유효기간은 30일로 설정합니다.
- **정중한 User-Agent**:
  ```http
  User-Agent: SkinMate-Crawler/0.1.0 (sosoj1552@gmail.com; project-skinmate; academic-use)
  ```
  - 크롤러 이름, 담당자 연락처, 프로젝트 명칭, 비상업 연구/학술 목적(academic-use)을 헤더에 명확히 표기합니다.

### 2.3 출처 메타데이터 (`source_meta`) 규격
수집된 모든 데이터 행(`ingredients`, `products`, `documents`)에는 JSONB 타입의 `source_meta`를 필수로 보존하여 데이터의 무결성을 입증합니다.

```json
{
  "url": "https://coos.kr/ingredients/123",
  "kind": "ingredient_detail",
  "crawled_at": "2026-07-08T14:20:00Z",
  "robots_ok": true
}
```
- `url` (string): 데이터를 수집한 원본 URL 주소.
- `kind` (string): 수집된 정보의 종류 (예: `ingredient_detail`, `beautypedia_prose`, `product_detail`).
- `crawled_at` (string): ISO 8601 형식의 수집 시각.
- `robots_ok` (boolean): `robots.txt`에 수집 거부 룰이 없음을 확인한 플래그.

---

## 3. 성분 정규화 및 canonical_key 결정 규칙

다양한 이름으로 입력되는 성분을 그래프 노드 및 하드 필터(`product_ingredients`)에서 단일화하기 위해, 성분 적재 시 아래 규칙에 따라 canonical_key를 생성합니다.

1. **INCI(국제 표준 명칭)가 존재하는 경우**:
   - INCI 명칭을 영문 **소문자화**합니다.
   - 모든 공백, 하이픈(-) 및 특수기호는 언더스코어(`_`)로 변경합니다.
   - 예: `Ascorbyl Glucoside` $\rightarrow$ `ascorbyl_glucoside`
   - 예: `Hyaluronic Acid` $\rightarrow$ `hyaluronic_acid`
2. **INCI가 존재하지 않는 경우**:
   - 정규화된 한글 성분명을 키로 생성합니다.
   - 괄호 안의 영문 표기 등은 모두 탈락시키고, 한글 공백을 언더스코어(`_`)로 치환합니다.
   - 예: `정제수 (Water)` $\rightarrow$ `정제수`
