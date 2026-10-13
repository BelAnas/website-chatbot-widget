"""The HTTP service: chat stream, leads, admin view, the widget file and the demo website."""
from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .config import Settings
from .engine import ChatEngine
from .store import Store

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    page_url: str = Field(default="", max_length=500)


def sse(event: dict) -> str:
    """One server-sent event: the event name on one line, the JSON payload on the next."""
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def create_app(settings: Settings | None = None, engine: ChatEngine | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if engine is None:
        from .cli import get_index

        engine = ChatEngine(settings, get_index(settings), Store(settings.db_path))
    store = engine.store
    app = FastAPI(title="Website chatbot", docs_url="/api/docs", redoc_url=None)
    app.state.settings, app.state.engine = settings, engine

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "site": settings.site_name, "provider": engine.provider.name,
                "search_mode": settings.search_mode, "index": engine.index.meta}

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
        if not _SESSION_ID.match(body.session_id):
            raise HTTPException(422, "session_id must be 8 to 64 letters, digits, - or _")
        message = body.message.strip()
        if not message:
            raise HTTPException(422, "The message is empty.")

        async def events() -> AsyncIterator[str]:
            try:
                async for event in engine.reply(body.session_id, message, body.page_url):
                    yield sse(event)
            except Exception as exc:  # noqa: BLE001 - the visitor must get a clean message, whatever broke
                print(f"chat error: {type(exc).__name__}: {exc}")
                yield sse({"type": "error", "message": "Sorry, I could not answer just now. Please try again in a moment."})

        # no-cache and X-Accel-Buffering stop proxies (nginx) from holding the stream back.
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
