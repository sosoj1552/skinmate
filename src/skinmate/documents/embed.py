"""임베딩 생성 스텁.

WBS 0.5 조기 납품. 1024차원 더미 벡터를 결정론적으로 반환.
"""

from __future__ import annotations

import hashlib


def embed_text(text: str) -> list[float]:
    """텍스트를 1024차원 벡터로 변환 (더미 스텁).

    결정론적인 테스트를 위해 입력 텍스트의 MD5 해시값을 시드로 난수 벡터를 생성합니다.
    """
    hasher = hashlib.md5(text.encode("utf-8"))
    digest = hasher.digest()

    vector = []
    for i in range(1024):
        # 16바이트 해시값을 활용해 결정론적 실수값 생성 [-1.0, 1.0]
        val = ((digest[i % 16] + i) % 256) / 128.0 - 1.0
        vector.append(val)

    return vector
