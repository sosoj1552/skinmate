"""GraphRAG LLM 트리플 추출 슬라이스 프로토타입 (docs/graphrag-design.md 검증용).

실제 아티클 청크 ~10개를 NVIDIA LLM(NvidiaProvider)으로 트리플(성분-작동원리-피부고민 관계)
추출 → 표면형 해소 → Apache AGE 그래프 적재 → W1 메타패스 순회까지의 전 과정을 콘솔 출력으로
증명한다. 추출 파이프라인이 실제로 쓸 만한 트리플을 만드는지 검증하는 것이 목적이다.

그래프 접근은 choke.age_exec 단일 관문만 경유한다(라벨 생성 DDL만 예외적으로 raw SQL,
scripts/graphrag_slice.py 와 동일한 패턴). 재실행해도 안전(MERGE 기반 멱등).

실행: POSTGRES_PASSWORD=change-me PYTHONIOENCODING=utf-8 \
      .venv/Scripts/python.exe scripts/graphrag_extract_slice.py
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

import psycopg
import structlog
from pydantic import BaseModel, ValidationError

from skinmate.config import settings
from skinmate.errors import LLMError
from skinmate.graph import choke
from skinmate.llm.nvidia import NvidiaProvider

logger = structlog.get_logger()

# --- 프롬프트 (사용자 지정, 그대로 사용) ---------------------------------------------

SYSTEM_PROMPT = (
    "너는 스킨케어 전문 문서에서 성분·작동원리·피부고민 사이의 '관계(트리플)'를 추출하는 "
    "도구다. 문서에 명시적으로 나타난 관계만 뽑고 추측하지 마라. 관계가 없으면 빈 배열을 "
    "반환한다."
)

EXTRACTION_INSTRUCTIONS = """아래 문서에서 다음 관계 트리플을 추출하라.

[노드 유형]
- ingredient: 구체적 성분명(레티놀, 살리실릭애씨드, 나이아신아마이드 등). BHA/AHA 같은 관용 표기도
  ingredient로 취급하되 문서 표면 표현 그대로 쓴다.
- mechanism: 작동원리(어떻게 작용하는가). 반드시 아래 11개 표준 목록 중 **가장 가까운 하나의
  단어**로만 표기하라: 각질제거, 보습, 진정, 피지조절, 모공관리, 항산화, 장벽강화, 미백,
  탄력개선, 자외선차단, 영양공급. 문장·구절로 쓰지 마라(예: "넓어진 모공을 축소시켜줍니다" ❌
  → "모공관리" ✅). 목록 어디에도 안 맞으면 그 mechanism 관계는 만들지 말고, 대신 성분→고민
  직접(TREATS) 관계로 표현하거나 생략하라.
- concern: 피부고민(여드름/트러블, 건조, 민감, 주름, 피지, 칙칙함, 모공 중 하나에 해당).

[관계 유형]
- ACHIEVES: ingredient → mechanism ("이 성분이 이 작용을 한다")
- ENABLES: mechanism → mechanism (작동원리 연쇄)
- TREATS: (mechanism|ingredient) → concern ("도움/완화")
- AGGRAVATES: (mechanism|ingredient) → concern ("악화/유발")
- HELPS: ingredient ↔ ingredient (같이 쓰면 좋음)
- CONFLICTS: ingredient ↔ ingredient (같이 쓰면 나쁨)

[규칙]
- 문서에 명시된 관계만. 추측·일반상식 금지.
- subject/object는 문서에 나온 표면 표현 그대로(한글).
- 행동·습관(세안·마사지 등)과 제품 제형은 제외. 오직 성분·작동원리·고민만.
- 해당 관계가 없으면 빈 배열.

문서:"""

TRIPLE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "triples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "subject_type": {"type": "string", "enum": ["ingredient", "mechanism"]},
                    "relation": {
                        "type": "string",
                        "enum": [
                            "ACHIEVES",
                            "ENABLES",
                            "TREATS",
                            "AGGRAVATES",
                            "HELPS",
                            "CONFLICTS",
                        ],
                    },
                    "object": {"type": "string"},
                    "object_type": {
                        "type": "string",
                        "enum": ["mechanism", "concern", "ingredient"],
                    },
                },
                "required": ["subject", "subject_type", "relation", "object", "object_type"],
            },
        }
    },
    "required": ["triples"],
}

RelationType = Literal["ACHIEVES", "ENABLES", "TREATS", "AGGRAVATES", "HELPS", "CONFLICTS"]
NodeType = Literal["ingredient", "mechanism", "concern"]


class Triple(BaseModel):
    subject: str
    subject_type: Literal["ingredient", "mechanism"]
    relation: RelationType
    object: str
    object_type: NodeType


# --- 해소 규칙 ------------------------------------------------------------------------

# 순서가 우선순위(포함 검사, 첫 매치)
CONCERN_KEYWORD_RULES: list[tuple[str, list[str]]] = [
    ("acne", ["여드름", "트러블", "여드름성", "뾰루지"]),
    ("dryness", ["건조", "건성"]),
    ("sensitivity", ["민감", "자극"]),
    ("wrinkles", ["주름", "탄력", "노화"]),
    ("oiliness", ["피지", "지성", "유분"]),
    ("dullness", ["칙칙", "미백", "색소", "피부톤"]),
    ("pores", ["모공"]),
]

CONCERN_DISPLAY_MAP = {
    "acne": "여드름",
    "dryness": "건조",
    "sensitivity": "민감",
    "wrinkles": "주름",
    "oiliness": "피지",
    "dullness": "칙칙함",
    "pores": "모공",
}

INGREDIENT_ALIASES: dict[str, int] = {
    "비타민c": 35,  # L-아스코빅애씨드
    "비타민씨": 35,
    "바하": 639,  # 살리실릭애씨드
    "bha": 639,
    "리퀴드바하": 639,
    "아하": 117,  # 글라이콜릭애씨드
    "aha": 117,
    "히알루론산": 829,  # 하이알루로닉애씨드
    "히알루론": 829,
    "변성알코올": 40,  # SD 알코올
    "알코올": 40,
    "sd알코올": 40,
    "세라마이드": 803,  # 세라마이드엔피 (대표형 단순화)
}

MECHANISM_STANDARD_LIST: tuple[str, ...] = (
    "각질제거",
    "보습",
    "진정",
    "피지조절",
    "모공관리",
    "항산화",
    "장벽강화",
    "미백",
    "탄력개선",
    "자외선차단",
    "영양공급",
)

MECHANISM_ALIASES: dict[str, str] = {
    "피지케어": "피지조절",
    "피지흡수": "피지조절",
    "피지흡착": "피지조절",
    "모공케어": "모공관리",
    "모공축소": "모공관리",
    "모공수축": "모공관리",
    "모공수렴": "모공관리",
    "피부톤개선": "미백",
    "브라이트닝": "미백",
    "수분공급": "보습",
    "수분": "보습",
    "보습력": "보습",
    "항산화작용": "항산화",
    "각질케어": "각질제거",
    "필링": "각질제거",
}


@dataclass
class ResolvedNode:
    label: str  # "Ingredient" | "Mechanism" | "Concern"
    key_prop: str  # "ingredient_id" | "name"
    key_value: int | str
    display: str


# --- DB 접속 (scripts/graphrag_slice.py 와 동일 패턴) ---------------------------------


def _db_url() -> str:
    user = os.getenv("POSTGRES_USER", "skinmate")
    password = os.getenv("POSTGRES_PASSWORD")
    if not password:
        logger.error("POSTGRES_PASSWORD is not set")
        raise SystemExit(1)
    db_name = os.getenv("POSTGRES_DB", "skinmate")
    port = os.getenv("POSTGRES_PORT", "5432")
    host = os.getenv("POSTGRES_HOST", "localhost")
    return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"


def ensure_labels(conn: psycopg.Connection[Any]) -> None:
    """Mechanism vlabel · ACHIEVES/ENABLES elabel 이 없으면 생성 (graphrag_slice.py 패턴)."""
    with conn.cursor() as cur:
        cur.execute("SET search_path = ag_catalog, public;")
        cur.execute("""
            DO $$
            DECLARE
                gid oid;
            BEGIN
                SELECT graphid INTO gid FROM ag_catalog.ag_graph WHERE name = 'skinmate';

                IF NOT EXISTS (
                    SELECT 1 FROM ag_catalog.ag_label WHERE name = 'Mechanism' AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_vlabel('skinmate', 'Mechanism');
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM ag_catalog.ag_label WHERE name = 'ACHIEVES' AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_elabel('skinmate', 'ACHIEVES');
                END IF;

                IF NOT EXISTS (
                    SELECT 1 FROM ag_catalog.ag_label WHERE name = 'ENABLES' AND graph = gid
                ) THEN
                    PERFORM ag_catalog.create_elabel('skinmate', 'ENABLES');
                END IF;
            END $$;
            """)
    logger.info("labels_ensured", vlabel="Mechanism", elabels=["ACHIEVES", "ENABLES"])


# --- 1) 대상 청크 선택 ------------------------------------------------------------------


def fetch_target_chunks(conn: psycopg.Connection[Any]) -> list[tuple[int, str]]:
    query = r"""
        SELECT doc_id, content FROM documents
        WHERE content !~ '^\[[^]]*참고 자료[^]]*\]' AND length(content) > 400
          AND (content LIKE '%여드름%' OR content LIKE '%각질%' OR content LIKE '%트러블%'
               OR content LIKE '%모공%' OR content LIKE '%피지%' OR content LIKE '%진정%')
        ORDER BY doc_id LIMIT 10;
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return [(int(doc_id), content) for doc_id, content in cur.fetchall()]


# --- 2) 트리플 추출 ---------------------------------------------------------------------


def parse_triples(raw: dict[str, object], doc_id: int) -> list[Triple]:
    raw_triples = raw.get("triples")
    if not isinstance(raw_triples, list):
        raise LLMError(f"'triples' 필드 누락/형식 오류: {raw!r}")

    triples: list[Triple] = []
    for item in raw_triples:
        try:
            triples.append(Triple.model_validate(item))
        except ValidationError as exc:
            # 온톨로지 밖 relation/type 등 스키마 위반 트리플만 skip (청크 전체는 살림)
            logger.warning(
                "triple_schema_violation_skipped", doc_id=doc_id, item=item, error=str(exc)
            )
    return triples


def extract_triples(provider: NvidiaProvider, doc_id: int, chunk_text: str) -> list[Triple]:
    """청크 1건에서 트리플을 추출한다. LLM 호출/파싱 실패 시 1회 재시도 후 skip."""
    user_prompt = f"{EXTRACTION_INSTRUCTIONS}\n{chunk_text}"

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            raw = provider.complete_json(SYSTEM_PROMPT, user_prompt, TRIPLE_SCHEMA)
            return parse_triples(raw, doc_id)
        except LLMError as exc:
            last_error = exc
            logger.warning(
                "triple_extraction_attempt_failed", doc_id=doc_id, attempt=attempt, error=str(exc)
            )

    logger.error("triple_extraction_failed_skip_chunk", doc_id=doc_id, error=str(last_error))
    print(f"  [ERROR] doc_id={doc_id} 추출 실패(1회 재시도 후 skip): {last_error}")
    return []


# --- 3) 표면형 해소 --------------------------------------------------------------------


def _normalize_surface(surface: str) -> str:
    """해소 전 표면형 정규화: 괄호와 그 내용 제거 + 내부/양끝 공백 제거 + 소문자화.

    "바하(BHA)" -> "바하", "아스코빅애씨드(15%)" -> "아스코빅애씨드", "시어 버터" -> "시어버터".
    """
    no_parens = re.sub(r"[（(][^）)]*[）)]", "", surface)
    return re.sub(r"\s+", "", no_parens).strip().lower()


def _lookup_ingredient_id(conn: psycopg.Connection[Any], normalized_surface: str) -> int | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ingredient_id FROM ingredients WHERE replace(lower(name_ko), ' ', '') = %s "
            "LIMIT 1;",
            (normalized_surface,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else None


def _lookup_ingredient_name(conn: psycopg.Connection[Any], ingredient_id: int) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name_ko FROM ingredients WHERE ingredient_id = %s LIMIT 1;",
            (ingredient_id,),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def resolve_ingredient(conn: psycopg.Connection[Any], surface: str) -> ResolvedNode | None:
    normalized = _normalize_surface(surface)
    if not normalized:
        return None

    ingredient_id = _lookup_ingredient_id(conn, normalized)
    display = surface.strip()
    if ingredient_id is None:
        ingredient_id = INGREDIENT_ALIASES.get(normalized)
        if ingredient_id is not None:
            display = _lookup_ingredient_name(conn, ingredient_id) or display

    if ingredient_id is None:
        return None
    return ResolvedNode(
        label="Ingredient", key_prop="ingredient_id", key_value=ingredient_id, display=display
    )


def resolve_mechanism(surface: str) -> ResolvedNode | None:
    normalized = _normalize_surface(surface)
    if not normalized:
        return None
    canonical = MECHANISM_ALIASES.get(normalized, normalized)
    if canonical not in MECHANISM_STANDARD_LIST:
        return None
    return ResolvedNode(label="Mechanism", key_prop="name", key_value=canonical, display=canonical)


def resolve_concern(surface: str) -> ResolvedNode | None:
    for code, keywords in CONCERN_KEYWORD_RULES:
        if any(kw in surface for kw in keywords):
            return ResolvedNode(label="Concern", key_prop="name", key_value=code, display=code)
    return None


def resolve_node(
    conn: psycopg.Connection[Any], node_type: NodeType, surface: str
) -> ResolvedNode | None:
    if node_type == "ingredient":
        return resolve_ingredient(conn, surface)
    if node_type == "mechanism":
        return resolve_mechanism(surface)
    return resolve_concern(surface)


def resolve_triple(
    conn: psycopg.Connection[Any], triple: Triple, skip_reasons: Counter[str]
) -> tuple[ResolvedNode, ResolvedNode] | None:
    subj = resolve_node(conn, triple.subject_type, triple.subject)
    if subj is None:
        skip_reasons[f"subject_{triple.subject_type}_unresolved"] += 1
        return None

    obj = resolve_node(conn, triple.object_type, triple.object)
    if obj is None:
        skip_reasons[f"object_{triple.object_type}_unresolved"] += 1
        return None

    return subj, obj


# --- 4) 그래프 적재 (choke.age_exec, origin='llm_doc', 확증 누적) -----------------------


def ensure_node(conn: psycopg.Connection[Any], node: ResolvedNode) -> None:
    if node.label == "Ingredient":
        choke.age_exec(
            conn,
            None,
            "MERGE (n:Ingredient {ingredient_id: $key_value}) SET n.name_ko = $display",
            {"key_value": node.key_value, "display": node.display},
        )
    else:
        choke.age_exec(
            conn,
            None,
            f"MERGE (n:{node.label} {{{node.key_prop}: $key_value}})",
            {"key_value": node.key_value},
        )


def _read_existing_source_docs(
    conn: psycopg.Connection[Any], match_pattern: str, params: dict[str, Any]
) -> list[int]:
    rows = choke.age_exec(conn, None, f"{match_pattern} RETURN r.source_doc_ids", params)
    if not rows:
        return []
    val: Any = rows[0]
    if isinstance(val, dict) and "value" in val:
        val = val["value"]
    if isinstance(val, list):
        return [int(d) for d in val]
    return []


def load_triple(
    conn: psycopg.Connection[Any],
    subj: ResolvedNode,
    obj: ResolvedNode,
    relation: RelationType,
    doc_id: int,
    edge_confidence: dict[str, int],
) -> None:
    """노드를 각각 MERGE로 바인딩 후 관계만 MERGE(존재 보장), 속성은 별도 MATCH+SET.

    ⚠️ AGE/Cypher 함정(graphrag_slice.py 문서화): 관계 패턴을 단일 MERGE로 묶으면 노드가
    재사용되지 않고 새로 생성되므로, 노드는 각각 별도 MERGE로 먼저 바인딩한다. 관계 속성도
    MERGE 직후 SET하면 영속화되지 않으므로 별도 MATCH+SET으로 분리한다.
    """
    ensure_node(conn, subj)
    ensure_node(conn, obj)

    merge_cypher = (
        f"MERGE (s:{subj.label} {{{subj.key_prop}: $s_key}}) "
        f"MERGE (o:{obj.label} {{{obj.key_prop}: $o_key}}) "
        f"MERGE (s)-[:{relation}]->(o)"
    )
    key_params = {"s_key": subj.key_value, "o_key": obj.key_value}
    choke.age_exec(conn, None, merge_cypher, key_params)

    match_pattern = (
        f"MATCH (s:{subj.label} {{{subj.key_prop}: $s_key}})"
        f"-[r:{relation}]->"
        f"(o:{obj.label} {{{obj.key_prop}: $o_key}})"
    )
    existing_docs = _read_existing_source_docs(conn, match_pattern, key_params)
    new_docs = sorted(set(existing_docs) | {doc_id})

    choke.age_exec(
        conn,
        None,
        f"{match_pattern} SET r.source_doc_ids = $source_doc_ids, "
        "r.origin = $origin, r.confidence = $confidence",
        {
            **key_params,
            "source_doc_ids": new_docs,
            "origin": "llm_doc",
            "confidence": len(new_docs),
        },
    )

    edge_key = f"{subj.label}:{subj.key_value} -[{relation}]-> {obj.label}:{obj.key_value}"
    edge_confidence[edge_key] = len(new_docs)


# --- 5) 검증 출력: W1 순회 재실행 --------------------------------------------------------

W1_QUERY = """
MATCH (c:Concern {name: $concern_name})<-[t:TREATS]-(m:Mechanism)<-[a:ACHIEVES]-(i:Ingredient)
RETURN {ing_id: i.ingredient_id, ing_name: i.name_ko, mech: m.name,
        treats_docs: t.source_doc_ids, achieves_docs: a.source_doc_ids}
"""


def traverse_w1(conn: psycopg.Connection[Any], concern_name: str) -> list[dict[str, Any]]:
    rows = choke.age_exec(conn, None, W1_QUERY, {"concern_name": concern_name})
    paths: list[dict[str, Any]] = []
    for row in rows:
        val = row.get("value", row) if isinstance(row, dict) else row
        if not isinstance(val, dict) or "ing_id" not in val:
            continue
        paths.append(val)
    return paths


def fetch_products(
    conn: psycopg.Connection[Any], ingredient_ids: list[int]
) -> list[tuple[int, str, str | None]]:
    if not ingredient_ids:
        return []
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.product_id, p.name, p.brand
            FROM products p
            JOIN product_ingredients pi ON pi.product_id = p.product_id
            WHERE pi.ingredient_id = ANY(%s);
            """,
            (ingredient_ids,),
        )
        return cur.fetchall()


def fetch_documents(conn: psycopg.Connection[Any], doc_ids: list[int]) -> dict[int, str]:
    if not doc_ids:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT doc_id, left(content, 200) FROM documents WHERE doc_id = ANY(%s);",
            (doc_ids,),
        )
        return dict(cur.fetchall())


def print_traversal(conn: psycopg.Connection[Any], concern_code: str, concern_display: str) -> None:
    print(f"\n=== W1 재순회: {concern_display} ({concern_code}) ===")
    print(f"질문: {concern_display}에 좋은 제품 추천")

    paths = traverse_w1(conn, concern_code)
    if not paths:
        print("추천 경로(이유): 0건 — 그래프 순회 결과 없음")
        return

    ingredient_ids: set[int] = set()
    doc_ids: set[int] = set()
    for p in paths:
        print(
            f"추천 경로(이유): {concern_display} ← [{p['mech']}가 도움] ← {p['mech']} "
            f"← [{p['ing_name']}가 수행] ← {p['ing_name']}"
        )
        ingredient_ids.add(int(p["ing_id"]))
        for docs_key in ("treats_docs", "achieves_docs"):
            docs = p.get(docs_key)
            if docs:
                doc_ids.update(int(d) for d in docs)

    documents = fetch_documents(conn, sorted(doc_ids))
    if not documents:
        print("근거 문서: 0건")
    for doc_id in sorted(documents):
        print(f'근거 문서: doc {doc_id} — "{documents[doc_id]}..."')

    products = fetch_products(conn, sorted(ingredient_ids))
    print(f"추천 제품({len(products)}개):")
    if not products:
        print("  (제품 0건)")
    for product_id, name, brand in products:
        print(f"  - product_id={product_id} {name} ({brand})")


def main() -> None:
    db_url = _db_url()
    provider = NvidiaProvider(api_key=settings.openai_api_key)
    logger.info("graphrag_extract_slice_started")

    success_count = 0
    skip_count = 0
    skip_reasons: Counter[str] = Counter()
    node_upsert_count = 0
    edge_load_count = 0
    edge_confidence: dict[str, int] = {}
    touched_concerns: set[str] = set()

    with psycopg.connect(db_url) as conn:
        ensure_labels(conn)
        conn.commit()

        chunks = fetch_target_chunks(conn)
        logger.info("target_chunks_fetched", count=len(chunks))

        for doc_id, content in chunks:
            print(f"\n--- chunk doc_id={doc_id} ---")
            triples = extract_triples(provider, doc_id, content)

            print(f"raw 추출 트리플 ({len(triples)}건):")
            if not triples:
                print("  (없음)")
            for t in triples:
                print(
                    f"  {t.subject}({t.subject_type}) --[{t.relation}]--> "
                    f"{t.object}({t.object_type})"
                )

            for t in triples:
                resolved = resolve_triple(conn, t, skip_reasons)
                if resolved is None:
                    skip_count += 1
                    continue
                subj, obj = resolved
                load_triple(conn, subj, obj, t.relation, doc_id, edge_confidence)
                success_count += 1
                node_upsert_count += 2
                edge_load_count += 1
                if obj.label == "Concern":
                    touched_concerns.add(str(obj.key_value))

            conn.commit()

        # --- 해소 요약 ---
        print(f"\n해소 요약: 성공 {success_count}건 / skip {skip_count}건")
        if skip_reasons:
            print("skip 사유 상위:")
            for reason, cnt in skip_reasons.most_common(10):
                print(f"  {reason}: {cnt}건")

        # --- 적재 요약 ---
        print(f"\n적재 요약: 노드 upsert 시도 {node_upsert_count}건, 엣지 적재 {edge_load_count}건")
        accumulated = [(k, v) for k, v in edge_confidence.items() if v > 1]
        if accumulated:
            print(f"확증 누적 엣지 (confidence>1, {len(accumulated)}건 중 최대 5건):")
            for key, conf in accumulated[:5]:
                print(f"  {key}: confidence={conf}")
        else:
            print("확증 누적 엣지: 없음 (모든 엣지가 단일 문서 근거)")

        # --- W1 순회 재실행 ---
        print_traversal(conn, "acne", CONCERN_DISPLAY_MAP["acne"])

        other_concern = next((c for c in sorted(touched_concerns) if c != "acne"), None)
        if other_concern:
            print_traversal(
                conn, other_concern, CONCERN_DISPLAY_MAP.get(other_concern, other_concern)
            )
        else:
            print("\n=== 추가 고민 W1 재순회 생략 ===")
            print("acne 외 다른 고민으로 적재된 트리플이 없어 생략함.")


if __name__ == "__main__":
    main()
