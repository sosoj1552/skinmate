"""skinmate 도메인 예외. 저수준 예외는 이들로 감싸 던진다(코딩 컨벤션)."""


class SkinmateError(Exception):
    """모든 skinmate 도메인 예외의 베이스."""


class ConfigError(SkinmateError):
    """설정/환경변수 누락·오류."""


class LLMError(SkinmateError):
    """LLM 호출 실패(파싱·타임아웃 등)."""


class GraphAccessError(SkinmateError):
    """AGE 그래프 접근 규약 위반(choke 우회 등)."""
