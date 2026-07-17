"""그래프 경로 계약 — 2+hop 순회 결과를 근거로 방출하기 위한 형식(⭐7, AC-G2).

근거: docs/DATA-MODEL.md §2(노드 5·엣지 원천 2갈래), PRD.md F4.
노드/엣지 종류는 그래프 온톨로지(db/migrations/003_graph_ontology.sql)와 일치.
경로는 순서 있는 노드 리스트 + 노드 인덱스를 참조하는 엣지 리스트로 표현한다.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, model_validator


class NodeKind(StrEnum):
    """그래프 노드 종류(DATA-MODEL §2). Season·Formulation 노드는 없음(임베딩 전용)."""

    USER = "User"
    INGREDIENT = "Ingredient"
    PRODUCT = "Product"
    CONCERN = "Concern"
    BRAND = "Brand"
    MECHANISM = "Mechanism"  # 성분 작동원리(GraphRAG 개념 기반 메타패스 W1)


class EdgeRel(StrEnum):
    """그래프 엣지 관계(DATA-MODEL §2). 전역(지식·containment) + 개인(memories 투영)."""

    CONTAINS = "CONTAINS"  # (:Product)->(:Ingredient), product_ingredients 투영
    TREATS = "TREATS"  # (:Ingredient|:Mechanism)->(:Concern), 그래프 네이티브
    AGGRAVATES = "AGGRAVATES"  # (:Ingredient)->(:Concern), 그래프 네이티브
    HELPS = "HELPS"  # (:Ingredient)->(:Ingredient), 그래프 네이티브
    CONFLICTS = "CONFLICTS"  # (:Ingredient)->(:Ingredient), 그래프 네이티브
    AVOIDS = "AVOIDS"  # (:User)->(:Ingredient|Brand), 개인 엣지
    PREFERS = "PREFERS"  # (:User)->(:Ingredient|Brand), 개인 엣지
    HAS_CONCERN = "HAS_CONCERN"  # (:User)->(:Concern) {season?}, 개인 엣지
    ACHIEVES = "ACHIEVES"  # (:Ingredient)->(:Mechanism), GraphRAG W1 메타패스
    ENABLES = "ENABLES"  # (:Mechanism)->(:Mechanism), 작동원리 연쇄(GraphRAG)


class GraphNode(BaseModel):
    """경로 상의 노드. key 는 노드 식별자(성분 canonical_key·concern name·제품 id 등)."""

    kind: NodeKind
    key: str
    label: str | None = None  # 근거 문장용 사람이 읽는 표기


class GraphEdge(BaseModel):
    """경로 상의 방향 엣지. from_idx/to_idx 는 같은 GraphPath.nodes 의 인덱스."""

    rel: EdgeRel
    from_idx: int
    to_idx: int
    season: str | None = None  # HAS_CONCERN 의 계절 프로퍼티
    source_doc_ids: list[int] = []  # 근거 문서 provenance(GraphRAG ACHIEVES/TREATS)


class GraphPath(BaseModel):
    """2+hop 순회 경로. 근거 생성이 이 경로만 인용한다(환각 금지, AC-R3)."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]

    @model_validator(mode="after")
    def _edges_reference_valid_nodes(self) -> GraphPath:
        """엣지의 from_idx/to_idx 가 nodes 범위 안이고 경로가 비지 않았는지 보장."""
        if not self.nodes:
            raise ValueError("GraphPath 는 최소 1개 노드를 가져야 한다")
        n = len(self.nodes)
        for e in self.edges:
            if not (0 <= e.from_idx < n and 0 <= e.to_idx < n):
                raise ValueError(
                    f"엣지 인덱스가 노드 범위를 벗어남: from={e.from_idx} to={e.to_idx} n={n}"
                )
        return self
