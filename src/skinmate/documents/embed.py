"""임베딩 생성 엔진 (WBS 1A.2).

환경변수에 따라 테스트용 스텁 모드(해시 기반 결정론적 벡터)와
실물 BAAI/bge-m3 로컬 모델(1024차원) 모드를 투명하게 전환합니다.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any, ClassVar

import structlog

logger = structlog.get_logger()


class EmbeddingModel:
    """bge-m3 모델을 싱글톤으로 관리하여 메모리 중복 적재를 방지합니다."""

    _model: ClassVar[Any | None] = None

    @classmethod
    def get_instance(cls) -> Any:
        """bge-m3 모델 객체의 싱글톤 인스턴스를 반환합니다."""
        if cls._model is None:
            # 실물 모델 로드
            from sentence_transformers import SentenceTransformer

            logger.info("loading_sentence_transformer_model", model_id="BAAI/bge-m3")
            cls._model = SentenceTransformer("BAAI/bge-m3")
        return cls._model


def _embed_stub(text: str) -> list[float]:
    """결정론적이고 빠른 테스트를 위한 해시 기반 1024차원 더미 벡터 생성."""
    hasher = hashlib.md5(text.encode("utf-8"))
    digest = hasher.digest()
    vector = []
    for i in range(1024):
        val = ((digest[i % 16] + i) % 256) / 128.0 - 1.0
        vector.append(val)
    return vector


def embed_text(text: str) -> list[float]:
    """입력 텍스트를 bge-m3 임베딩 모델(1024차원)로 인코딩합니다.

    기본은 실물 모델이다. 스텁은 SKINMATE_EMBED_STUB=true 를 **명시한 경우에만** 켜진다
    (테스트·CI 전용). 예전에는 기본값이 스텁이어서, 적재는 실물 벡터인데 서버의 질의만
    해시 더미 벡터로 인코딩되는 바람에 유사도 검색 전체가 무작위 순위가 되는 결함이 있었다
    — 같은 이유로 실물 모델 로드 실패 시에도 스텁으로 조용히 폴백하지 않고 예외를 낸다
    (무작위 검색이 침묵 속에 재발하는 것 방지).
    """
    stub_mode = os.getenv("SKINMATE_EMBED_STUB", "false").lower() == "true"

    if stub_mode:
        return _embed_stub(text)

    try:
        model = EmbeddingModel.get_instance()
        # sentence-transformers는 기본적으로 float32 ndarray를 리턴하므로 float 리스트로 전환
        embeddings = model.encode([text])
        return [float(x) for x in embeddings[0]]
    except Exception as e:
        logger.error("real_embedding_model_unavailable", error=str(e))
        raise
