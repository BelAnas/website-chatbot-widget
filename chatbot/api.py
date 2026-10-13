"""The HTTP service: chat stream, leads, admin view, the widget file and the demo website."""
from __future__ import annotations

import asyncio
import csv
import hmac
import io
import json
import re
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import ROOT, Settings
from .engine import ChatEngine
from .guards import RateLimiter, origin_allowed
from .leads import send_webhook, validate_lead, webhook_payload
from .store import Store

STATIC = Path(__file__).resolve().parent / "static"
DEMO_SITE = ROOT / "demo_site"
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_CORS_HEADERS = {"Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                 "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Max-Age": "600"}


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    message: str = Field(min_length=1, max_length=4000)
    page_url: str = Field(default="", max_length=500)


class LeadRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    message: str = ""
    consent: bool = False
    page_url: str = Field(default="", max_length=500)
    # Hidden field that a person never sees. A form-filling bot fills it and gives itself away.
    website: str = ""


def sse(event: dict) -> str:
    """One server-sent event: the event name on one line, the JSON payload on the next."""
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


def _csv_safe(value: object) -> str:
    """A cell starting with = + - @ is run as a formula by spreadsheet programs: defuse it."""
    text = str(value)
    return "'" + text if text and text[0] in "=+-@\t\r" else text


def create_app(settings: Settings | None = None, engine: ChatEngine | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    if engine is None:
        from .cli import get_index

        engine = ChatEngine(settings, get_index(settings), Store(settings.db_path))
    store = engine.store
    admin_token = settings.admin_token or secrets.token_urlsafe(12)
    chat_limiter = RateLimiter(settings.rate_limit_per_minute)
    lead_limiter = RateLimiter(5)
    app = FastAPI(title="Website chatbot", docs_url="/api/docs", redoc_url=None)
    app.state.settings, app.state.engine, app.state.admin_token = settings, engine, admin_token
    app.state.webhook_client = None        # tests put a fake HTTP client here
    app.state.webhook_backoff_s = 1.0
    background: set[asyncio.Task] = set()  # keeps running webhook tasks from being garbage collected

    # ---- leads to the CRM ----
    async def deliver(lead_id: int) -> None:
        lead = store.lead(lead_id)
        if not lead or not settings.lead_webhook_url:
            return
        status, attempts = await send_webhook(settings.lead_webhook_url, settings.lead_webhook_secret,
                                              webhook_payload(lead, settings.site_name),
                                              backoff_s=app.state.webhook_backoff_s, client=app.state.webhook_client)
        store.set_webhook_status(lead_id, status, attempts)

    def deliver_later(lead_id: int) -> None:
        task = asyncio.get_running_loop().create_task(deliver(lead_id))
        background.add(task)
        task.add_done_callback(background.discard)

    engine.on_lead = deliver_later
    app.state.background = background

    # ---- guards ----
    def client_ip(request: Request) -> str:
        if settings.trust_proxy and request.headers.get("x-forwarded-for"):
            return request.headers["x-forwarded-for"].split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    @app.middleware("http")
    async def cross_origin(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Answers only the websites on the allow-list (and pages served by this service itself)."""
        origin = request.headers.get("origin")
        if not request.url.path.startswith("/api/") or request.url.path.startswith("/api/admin"):
            return await call_next(request)    # files are public; the admin API is same-origin only
        if not origin_allowed(origin, request.headers.get("host", ""), settings.allowed_origins):
            return JSONResponse({"detail": "This website is not allowed to use this chat service."}, status_code=403)
        response = Response(status_code=204) if request.method == "OPTIONS" else await call_next(request)
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers.update(_CORS_HEADERS)
        return response

    def check_session(session_id: str) -> None:
        if not _SESSION_ID.match(session_id):
            raise HTTPException(422, "session_id must be 8 to 64 letters, digits, - or _")

    def require_admin(request: Request) -> None:
        given = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
        if not hmac.compare_digest(given.encode(), admin_token.encode()):
            raise HTTPException(401, "Admin token missing or wrong.")

    # ---- public API ----
    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "site": settings.site_name, "provider": engine.provider.name,
                "search_mode": settings.search_mode, "index": engine.index.meta}

    @app.get("/api/widget-config")
    def widget_config() -> dict:
        return {"site_name": settings.site_name, "greeting": settings.greeting,
                "suggestions": settings.suggested_questions, "max_message_chars": settings.max_message_chars}

    @app.post("/api/chat")
    async def chat(body: ChatRequest, request: Request) -> StreamingResponse:
        check_session(body.session_id)
        message = body.message.strip()
        if not message:
            raise HTTPException(422, "The message is empty.")
        # Every check below happens before a search or a model call, so a rejected request costs nothing.
        if len(message) > settings.max_message_chars:
            raise HTTPException(413, f"Please keep your message under {settings.max_message_chars} characters.")
        allowed, wait = chat_limiter.allow(client_ip(request))
        if not allowed:
            raise HTTPException(429, "You are sending messages very quickly. Please wait a moment.",
                                headers={"Retry-After": str(wait)})
        if store.user_messages_today() >= settings.daily_message_budget:
            raise HTTPException(429, "The assistant has reached its limit for today. Please use the contact page.")

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

    @app.post("/api/leads")
    async def create_lead(body: LeadRequest, request: Request) -> JSONResponse:
        check_session(body.session_id)
        allowed, wait = lead_limiter.allow(client_ip(request))
        if not allowed:
            raise HTTPException(429, "Too many submissions. Please wait a moment.", headers={"Retry-After": str(wait)})
        if body.website:
            return JSONResponse({"ok": True})     # a bot: say thanks, store nothing
        clean, errors = validate_lead(body.model_dump())
        if errors:
            return JSONResponse({"ok": False, "errors": errors}, status_code=422)
        session = store.touch_session(body.session_id, body.page_url)
        lead_id = store.add_lead(body.session_id, clean["email"], name=clean["name"], phone=clean["phone"],
                                 company=clean["company"], message=clean["message"], consent=True, source="form",
                                 reason=session["lead_reason"] or "form", page_url=body.page_url,
                                 interest=store.session_topics(body.session_id))
        deliver_later(lead_id)
        return JSONResponse({"ok": True, "lead_id": lead_id})

    # ---- admin ----
    @app.get("/api/admin/overview")
    def admin_overview(request: Request) -> dict:
        require_admin(request)
        return {"site": settings.site_name, "stats": store.stats(), "leads": store.leads(),
                "unanswered": store.unanswered(), "webhook_configured": bool(settings.lead_webhook_url)}

    @app.get("/api/admin/leads.csv")
    def admin_leads_csv(request: Request) -> PlainTextResponse:
        require_admin(request)
        columns = ["id", "created_at", "name", "email", "phone", "company", "reason", "interest", "message",
                   "source", "consent", "page_url", "webhook_status"]
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(columns)
        for lead in store.leads():
            writer.writerow([_csv_safe(lead[c]) for c in columns])
        return PlainTextResponse(out.getvalue(), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=leads.csv"})

    # ---- files ----
    @app.get("/widget.js")
    def widget() -> FileResponse:
        # Short cache: a site owner gets widget fixes within five minutes without touching their page.
        return FileResponse(STATIC / "widget.js", media_type="text/javascript",
                            headers={"Cache-Control": "public, max-age=300"})

    @app.get("/admin")
    def admin_page() -> FileResponse:
        return FileResponse(STATIC / "admin.html", media_type="text/html")

    if DEMO_SITE.is_dir():
        def demo_text(name: str, request: Request, media_type: str) -> Response:
            base = str(request.base_url).rstrip("/") + "/demo"
            return Response((DEMO_SITE / name).read_text(encoding="utf-8").replace("{base}", base), media_type=media_type)

        # Served at the root too, because that is where a crawler looks for them.
        @app.get("/robots.txt")
        @app.get("/demo/robots.txt")
        def robots(request: Request) -> Response:
            return demo_text("robots.txt", request, "text/plain")

        @app.get("/sitemap.xml")
        @app.get("/demo/sitemap.xml")
        def sitemap(request: Request) -> Response:
            return demo_text("sitemap.xml", request, "application/xml")

        @app.get("/")
        def home() -> RedirectResponse:
            return RedirectResponse("/demo/index.html")

        app.mount("/demo", StaticFiles(directory=DEMO_SITE, html=True), name="demo")

    return app
