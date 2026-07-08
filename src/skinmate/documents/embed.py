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

    환경 변수 SKINMATE_EMBED_STUB=true 일 때 혹은 라이브러리가 없을 때
    스텁 모드로 폴백 작동하여 CI의 안전과 격리를 보장합니다.
    """
    stub_mode = os.getenv("SKINMATE_EMBED_STUB", "true").lower() == "true"

    if stub_mode:
        return _embed_stub(text)

    try:
        model = EmbeddingModel.get_instance()
        # sentence-transformers는 기본적으로 float32 ndarray를 리턴하므로 float 리스트로 전환
        embeddings = model.encode([text])
        return [float(x) for x in embeddings[0]]
    except Exception as e:
        logger.error("failed_to_load_real_embedding_model_falling_back_to_stub", error=str(e))
        return _embed_stub(text)
