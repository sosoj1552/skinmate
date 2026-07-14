import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from skinmate import db
from skinmate.app.turn import process_turn
from skinmate.chat.orchestrator import TurnResult
from skinmate.config import settings
from skinmate.documents.embed import embed_text
from skinmate.llm.base import LLMProvider
from skinmate.llm.nvidia import NvidiaProvider

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """기동 시 임베딩 모델 워밍업 — 첫 사용자 요청이 모델 로딩(~수십 초)을 뒤집어쓰지 않게
    하고, 스텁 모드로 잘못 떠 있으면(검색이 무작위가 됨) 로그로 즉시 표면화한다."""
    stub_mode = os.getenv("SKINMATE_EMBED_STUB", "false").lower() == "true"
    if stub_mode:
        logger.warning("embedder_running_in_stub_mode_search_quality_degraded")
    else:
        embed_text("서버 기동 워밍업")
        logger.info("embedder_warmed_up", model="bge-m3")
    yield


app = FastAPI(title="skinmate", lifespan=_lifespan)

# static 폴더 경로 설정 및 생성 보장
current_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(current_dir, "static")
os.makedirs(static_dir, exist_ok=True)

# 정적 파일 마운트
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
def read_index() -> FileResponse:
    return FileResponse(os.path.join(static_dir, "index.html"))


class ChatRequest(BaseModel):
    user_id: int
    utterance: str
    history: list[str] | None = None
    season: str | None = None


def _get_provider() -> LLMProvider:
    return NvidiaProvider(api_key=settings.openai_api_key, model=settings.llm_model)


@app.post("/chat")
def chat(req: ChatRequest) -> TurnResult:
    provider = _get_provider()
    conn = db.connect()
    try:
        result = process_turn(
            conn,
            provider,
            req.user_id,
            req.utterance,
            history=req.history,
            season=req.season,
        )
    finally:
        conn.close()
    return result
