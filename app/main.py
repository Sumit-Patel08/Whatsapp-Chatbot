"""FastAPI app: webhook routes, health check, and background task entry."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from redis.asyncio import Redis

from app.admin import router as admin_router
from app.brain import Brain, GeminiClient
from app.config import Settings, get_settings
from app.handlers import Bot
from app.speech import Speech
from app.store import Store
from app.whatsapp import WhatsAppClient, verify_signature

log = logging.getLogger("krishi_sakhi")


def create_app(
    settings: Settings | None = None,
    store: Store | None = None,
    bot: Bot | None = None,
) -> FastAPI:
    """Build the app. Tests pass their own settings/store/bot; production builds real ones."""
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    _init_sentry(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http: httpx.AsyncClient | None = None
        redis: Redis | None = None
        if app.state.bot is None:
            _warn_missing_settings(settings)
            redis = Redis.from_url(
                settings.redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=10,
                health_check_interval=30,  # hosted Redis (e.g. Upstash) drops idle connections
            )
            http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
            app.state.store = Store(redis)
            wa = WhatsAppClient(http, settings.wa_access_token, settings.wa_phone_number_id, settings.graph_api_version)
            gemini = GeminiClient(settings.gemini_api_key)  # shared by answers and voice
            brain = Brain(gemini, settings.gemini_model, settings.gemini_thinking_level)
            speech = Speech(
                gemini, settings.gemini_stt_model, settings.gemini_tts_model,
                settings.gemini_tts_voice, settings.gemini_thinking_level,
            )
            app.state.bot = Bot(settings, app.state.store, wa, brain, speech)
        try:
            yield
        finally:
            # Let in-flight replies finish (Railway/Cloud Run send SIGTERM first).
            await app.state.bot.drain(timeout=25)
            if http is not None:
                await http.aclose()
            if redis is not None:
                await redis.aclose()

    app = FastAPI(title="Krishi Sakhi", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.store = store if store is not None else (bot.store if bot else None)
    app.state.bot = bot
    app.include_router(admin_router)

    @app.get("/health")
    async def health() -> JSONResponse:
        try:
            await asyncio.wait_for(app.state.store.ping(), timeout=3)
        except Exception as exc:
            log.error("health: redis unreachable: %s", exc)
            return JSONResponse({"ok": False, "redis": "unreachable"}, status_code=503)
        return JSONResponse({"ok": True})

    @app.get("/webhook")
    async def verify_webhook(
        mode: str = Query("", alias="hub.mode"),
        token: str = Query("", alias="hub.verify_token"),
        challenge: str = Query("", alias="hub.challenge"),
    ) -> Response:
        if mode == "subscribe" and settings.wa_verify_token and token == settings.wa_verify_token:
            return PlainTextResponse(challenge)
        return PlainTextResponse("forbidden", status_code=403)

    @app.post("/webhook")
    async def receive_webhook(request: Request) -> Response:
        body = await request.body()
        if not verify_signature(settings.wa_app_secret, body, request.headers.get("X-Hub-Signature-256")):
            log.warning("webhook: bad or missing signature")
            return PlainTextResponse("invalid signature", status_code=403)
        try:
            payload: Any = json.loads(body)
        except ValueError:
            return PlainTextResponse("bad json", status_code=400)
        # Answer Meta right away; all AI work happens in the background.
        app.state.bot.schedule(payload)
        return PlainTextResponse("ok")

    return app


def _init_sentry(settings: Settings) -> None:
    """Optional error alerts. Only active if SENTRY_DSN is set *and* sentry-sdk is installed."""
    if not settings.sentry_dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        log.warning("SENTRY_DSN is set but sentry-sdk is not installed; skipping")
        return
    sentry_sdk.init(dsn=settings.sentry_dsn, environment=settings.app_env, send_default_pii=False)


def _warn_missing_settings(settings: Settings) -> None:
    required = {
        "WA_ACCESS_TOKEN": settings.wa_access_token,
        "WA_PHONE_NUMBER_ID": settings.wa_phone_number_id,
        "WA_APP_SECRET": settings.wa_app_secret,
        "WA_VERIFY_TOKEN": settings.wa_verify_token,
        "GEMINI_API_KEY": settings.gemini_api_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        log.warning("missing settings: %s", ", ".join(missing))
    if settings.is_production and settings.admin_api_key.startswith("change-me"):
        log.warning("ADMIN_API_KEY still has the example value; change it")


app = create_app()
