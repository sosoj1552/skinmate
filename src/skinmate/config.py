"""환경변수 기반 설정. `.env`를 로드한다."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """앱 전역 설정. 접속은 RLS 적용을 위해 비-superuser 역할(skinmate_app)로."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://skinmate_app:skinmate-app-dev-only@localhost:5432/skinmate"
    gemini_api_key: str = ""  # 기본 LLM(Google AI Studio 무료 키)
    anthropic_api_key: str = ""  # Claude 대체 구현 사용 시
    embedder_mode: str = "local"  # local | container | api  (⭐9d)
    embedder_endpoint: str = ""
    crawl_rate_limit: float = 1.5
    llm_model: str = "gemini-2.5-flash"


settings = Settings()
