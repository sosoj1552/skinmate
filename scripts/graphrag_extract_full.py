"""GraphRAG LLM 트리플 추출 전체 청크 확장 실행 (docs/graphrag-design.md 스케일 검증용).

graphrag_extract_slice.py 는 손 큐레이션 키워드로 거른 10개 청크만 다뤘다. 이 스크립트는
동일한 프롬프트/스키마/해소/적재 로직을 그대로 재사용하되, 대상을 "참고 자료 제외 전체
프로즈 청크"로 확장해 순수 LLM 추출의 실제 규모·품질·커버리지를 측정한다.

사전 조건: scripts/graphrag_rebuild.py 로 그래프를 비우고 7고민만 시드한 "깨끗한" 상태에서
실행해야 한다(손 큐레이션 슬라이스는 이번 실행에 포함하지 않는다).

그래프 접근은 choke.age_exec 단일 관문만 경유한다(graphrag_extract_slice.py 와 동일 패턴).
청크별 raw 트리플은 출력하지 않고 집계만 한다(전체 청크 기준으로 출력이 너무 길어짐).
NVIDIA 호출은 순차 실행하며 40 RPM 을 넘지 않도록 호출마다 짧은 sleep 을 둔다.

실행: POSTGRES_PASSWORD=change-me PYTHONIOENCODING=utf-8 \
      .venv/Scripts/python.exe scripts/graphrag_extract_full.py
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

import psycopg
import structlog
from graphrag_extract_slice import (  # type: ignore[import-not-found]
    CONCERN_DISPLAY_MAP,
    EXTRACTION_INSTRUCTIONS,
    SYSTEM_PROMPT,
    TRIPLE_SCHEMA,
    ResolvedNode,
    Triple,
    _db_url,
    ensure_labels,
    load_triple,
    parse_triples,
    print_traversal,
    resolve_node,
    traverse_w1,
)
from graphrag_rebuild import EDGE_TYPE_COUNT_QUERY  # type: ignore[import-not-found]

from skinmate.config import settings
from skinmate.errors import LLMError
from skinmate.graph import choke
from skinmate.llm.nvidia import NvidiaProvider

logger = structlog.get_logger()

# 40 RPM 을 넘지 않도록 호출(성공/실패 무관)마다 최소 간격을 둔다 (60/40=1.5s + 여유).
RATE_LIMIT_INTERVAL_SEC = 1.7


# --- 1) 대상 청크: 참고 자료 제외 전체 프로즈 (키워드 필터 없음) -----------------------


def fetch_target_chunks_full(conn: psycopg.Connection[Any]) -> list[tuple[int, str]]:
    query = r"""
        SELECT doc_id, content FROM documents
        WHERE content !~ '^\[[^]]*참고 자료[^]]*\]' AND length(content) > 300
        ORDER BY doc_id;
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return [(int(doc_id), content) for doc_id, content in cur.fetchall()]


# --- 2) 트리플 추출 (레이트리밋 + raw/스키마위반 집계 포함) -----------------------------


def extract_triples_with_stats(
    provider: NvidiaProvider, doc_id: int, chunk_text: str
) -> tuple[list[Triple], int, bool]:
    """청크 1건 추출. slice.py의 extract_triples와 동일한 재시도 정책이되, raw 트리플 수와
    성공 여부까지 반환해 스케일 지표 집계에 쓴다."""
    user_prompt = f"{EXTRACTION_INSTRUCTIONS}\n{chunk_text}"

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            raw = provider.complete_json(SYSTEM_PROMPT, user_prompt, TRIPLE_SCHEMA)
        except LLMError as exc:
            last_error = exc
            logger.warning(
                "triple_extraction_attempt_failed", doc_id=doc_id, attempt=attempt, error=str(exc)
            )
            time.sleep(RATE_LIMIT_INTERVAL_SEC)
            continue

        time.sleep(RATE_LIMIT_INTERVAL_SEC)
        raw_triples = raw.get("triples")
        raw_count = len(raw_triples) if isinstance(raw_triples, list) else 0
        triples = parse_triples(raw, doc_id)
        return triples, raw_count, True

    logger.error("triple_extraction_failed_skip_chunk", doc_id=doc_id, error=str(last_error))
    print(f"  [ERROR] doc_id={doc_id} 추출 실패(1회 재시도 후 skip): {last_error}")
    return [], 0, False


# --- 3) 표면형 해소 (미해소 성분 표면형 추적 포함) --------------------------------------


def resolve_triple_with_tracking(
    conn: psycopg.Connection[Any],
    triple: Triple,
    skip_reasons: Counter[str],
    unresolved_ingredient_surfaces: Counter[str],
) -> tuple[ResolvedNode, ResolvedNode] | None:
    subj = resolve_node(conn, triple.subject_type, triple.subject)
    if subj is None:
        skip_reasons[f"subject_{triple.subject_type}_unresolved"] += 1
        if triple.subject_type == "ingredient":
            unresolved_ingredient_surfaces[triple.subject] += 1
        return None

    obj = resolve_node(conn, triple.object_type, triple.object)
    if obj is None:
        skip_reasons[f"object_{triple.object_type}_unresolved"] += 1
        if triple.object_type == "ingredient":
            unresolved_ingredient_surfaces[triple.object] += 1
        return None

    return subj, obj


# --- 4) 그래프 결과 집계 쿼리 -----------------------------------------------------------


def count_vertices(conn: psycopg.Connection[Any], label: str) -> int:
    rows = choke.age_exec(conn, None, f"MATCH (n:{label}) RETURN count(n)")
    if not rows:
        return 0
    val: Any = rows[0]
    if isinstance(val, dict) and "value" in val:
        val = val["value"]
    return int(val)


def fetch_mechanism_degrees(conn: psycopg.Connection[Any]) -> list[tuple[str, int]]:
    rows = choke.age_exec(
        conn,
        None,
        "MATCH (m:Mechanism) OPTIONAL MATCH (m)<-[a:ACHIEVES]-() "
        "WITH m, count(a) AS achieves_count "
        "RETURN {name: m.name, achieves_count: achieves_count}",
    )
    out: list[tuple[str, int]] = []
    for row in rows:
        val = row.get("value", row) if isinstance(row, dict) else row
        if not isinstance(val, dict) or "name" not in val:
            continue
        out.append((str(val["name"]), int(val["achieves_count"])))
    out.sort(key=lambda kv: kv[1], reverse=True)
    return out


def treats_count(conn: psycopg.Connection[Any], concern_code: str) -> int:
    rows = choke.age_exec(
        conn,
        None,
        "MATCH (c:Concern {name: $name})<-[t:TREATS]-() RETURN count(t)",
        {"name": concern_code},
    )
    if not rows:
        return 0
    val: Any = rows[0]
    if isinstance(val, dict) and "value" in val:
        val = val["value"]
    return int(val)


# --- main --------------------------------------------------------------------------


def main() -> None:
    db_url = _db_url()
    provider = NvidiaProvider(api_key=settings.openai_api_key)
    logger.info("graphrag_extract_full_started")

    total_raw_triples = 0
    schema_violation_count = 0
    extraction_failed_chunks = 0
    success_count = 0
    skip_count = 0
    skip_reasons: Counter[str] = Counter()
    unresolved_ingredient_surfaces: Counter[str] = Counter()
    edge_confidence: dict[str, int] = {}

    with psycopg.connect(db_url) as conn:
        ensure_labels(conn)
        conn.commit()

        chunks = fetch_target_chunks_full(conn)
        total_chunks = len(chunks)
        logger.info("target_chunks_fetched", count=total_chunks)
        print(f"대상 청크 수: {total_chunks}건 (참고 자료 제외, length>300)")

        start_time = time.monotonic()
        for idx, (doc_id, content) in enumerate(chunks, start=1):
            triples, raw_count, ok = extract_triples_with_stats(provider, doc_id, content)
            if not ok:
                extraction_failed_chunks += 1
            total_raw_triples += raw_count
            schema_violation_count += max(raw_count - len(triples), 0)

            for t in triples:
                resolved = resolve_triple_with_tracking(
                    conn, t, skip_reasons, unresolved_ingredient_surfaces
                )
                if resolved is None:
                    skip_count += 1
                    continue
                subj, obj = resolved
                load_triple(conn, subj, obj, t.relation, doc_id, edge_confidence)
                success_count += 1

            conn.commit()

            if idx % 10 == 0 or idx == total_chunks:
                elapsed = time.monotonic() - start_time
                print(
                    f"[진행] {idx}/{total_chunks} 청크 처리 완료 "
                    f"(raw={total_raw_triples}, 성공={success_count}, skip={skip_count}, "
                    f"경과={elapsed:.0f}s)",
                    flush=True,
                )

        # === 최종 리포트 ===
        print("\n" + "=" * 80)
        print("=== 스케일 지표 리포트 ===")
        print("=" * 80)

        print("\n--- 1) 처리 규모 ---")
        print(
            f"처리한 청크 수: {total_chunks}건 (추출 완전 실패 {extraction_failed_chunks}건 포함)"
        )
        print(f"총 raw 트리플 수: {total_raw_triples}건")
        total_attempted = success_count + skip_count
        rate = (success_count / total_attempted * 100) if total_attempted else 0.0
        print(
            f"해소 성공: {success_count}건 / skip: {skip_count}건 "
            f"(성공률 {rate:.1f}%, 분모=해소 시도된 트리플 {total_attempted}건)"
        )
        print(
            f"스키마 위반(스키마 밖 relation/type)으로 애초에 버려진 트리플: "
            f"{schema_violation_count}건"
        )

        print("\n--- 2) skip 사유 분류 ---")
        if skip_reasons:
            for reason, cnt in skip_reasons.most_common():
                print(f"  {reason}: {cnt}건")
        else:
            print("  (skip 없음)")
        print(f"  schema_violation (스키마 밖 relation/type): {schema_violation_count}건")

        print("\n--- 3) 미해소 성분 표면형 top 30 ---")
        if unresolved_ingredient_surfaces:
            for surface, cnt in unresolved_ingredient_surfaces.most_common(30):
                print(f"  {surface!r}: {cnt}회")
        else:
            print("  (미해소 성분 없음)")

        print("\n--- 4) 그래프 결과 ---")
        ing_count = count_vertices(conn, "Ingredient")
        mech_count = count_vertices(conn, "Mechanism")
        concern_count = count_vertices(conn, "Concern")
        print(f"노드 수: Ingredient={ing_count}, Mechanism={mech_count}, Concern={concern_count}")

        edge_rows = choke.age_exec(conn, None, EDGE_TYPE_COUNT_QUERY)
        print("엣지 타입별 수:")
        if edge_rows:
            for row in sorted(edge_rows, key=lambda r: str(r.get("rel_type"))):
                print(f"  {row.get('rel_type')}: {row.get('cnt')}건")
        else:
            print("  (엣지 없음)")

        mech_degrees = fetch_mechanism_degrees(conn)
        print(f"서로 다른 Mechanism 표면형 개수(파편화 정도): {len(mech_degrees)}종")
        print("Mechanism 표면형별 ACHIEVES 수신 수(상위 30, 파편화 확인용):")
        for name, cnt in mech_degrees[:30]:
            print(f"  {name!r}: {cnt}건")

        print("\n--- 5) 커버리지 (7고민 기준) ---")
        concern_with_treats: list[str] = []
        concern_with_w1: list[str] = []
        for code, display in CONCERN_DISPLAY_MAP.items():
            t_cnt = treats_count(conn, code)
            w1_paths = traverse_w1(conn, code)
            ing_ids = {int(p["ing_id"]) for p in w1_paths}
            if t_cnt > 0:
                concern_with_treats.append(code)
            if w1_paths:
                concern_with_w1.append(code)
            print(
                f"  {display}({code}): TREATS 수신={t_cnt}건, W1 경로={len(w1_paths)}건, "
                f"추천 성분 수={len(ing_ids)}개"
            )
        print(f"TREATS 경로가 붙은 고민 수: {len(concern_with_treats)}/7 {concern_with_treats}")
        print(f"W1(성분→작동원리→고민) 경로가 성립하는 고민: {concern_with_w1}")

        print("\n--- 6) 확증(confidence>=2) ---")
        accumulated = sorted(
            ((k, v) for k, v in edge_confidence.items() if v >= 2),
            key=lambda kv: kv[1],
            reverse=True,
        )
        print(f"confidence>=2 엣지 수: {len(accumulated)}건")
        if accumulated:
            print("상위 확증 엣지 예:")
            for key, conf in accumulated[:10]:
                print(f"  {key}: confidence={conf}")

        print("\n--- 7) W1 샘플 (acne, dryness) ---")
        print_traversal(conn, "acne", CONCERN_DISPLAY_MAP["acne"])
        print_traversal(conn, "dryness", CONCERN_DISPLAY_MAP["dryness"])


if __name__ == "__main__":
    main()
