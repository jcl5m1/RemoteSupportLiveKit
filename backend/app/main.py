"""FastAPI entrypoint."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import structlog
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from google.cloud import storage as gcs
from livekit import api
from sqlalchemy import select

from .config import get_settings
from .db import AsyncSessionLocal, engine
from .degraded_clients import DegradedGCSClient, DegradedLiveKitAPI
from .models import Session, SessionState
from .routers import agent, recordings, sessions, transcripts, webhooks

logger = logging.getLogger("remote-support-backend")


def _configure_logging(log_level: str) -> None:
    """Structured JSON logging. Never log transcript text, tokens, or join codes."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(message)s",
        handlers=[logging.StreamHandler()],
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, log_level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


async def _sweep_idle_sessions(app: FastAPI, interval_seconds: int) -> None:
    """End sessions that have been active without updates for too long."""
    while True:
        await asyncio.sleep(interval_seconds)
        settings = get_settings()
        cutoff = datetime.now(UTC) - timedelta(seconds=settings.idle_session_timeout_seconds)
        try:
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Session).where(
                        Session.state == SessionState.ACTIVE,
                        Session.updated_at < cutoff,
                    )
                )
                stale_sessions = result.scalars().all()
                for session in stale_sessions:
                    session.state = SessionState.COMPLETED
                    session.ended_at = datetime.now(UTC)
                    try:
                        await app.state.lkapi.room.delete_room(
                            api.DeleteRoomRequest(room=session.room_name)
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "sweeper failed to delete room %s: %s", session.room_name, exc
                        )
                await db.commit()
                if stale_sessions:
                    logger.info(
                        "idle session sweeper closed %d session(s)", len(stale_sessions)
                    )
        except Exception:  # noqa: BLE001
            logger.exception("idle session sweeper failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings.log_level)

    try:
        app.state.lkapi = api.LiveKitAPI(
            settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret
        )
    except Exception as exc:  # noqa: BLE001
        if not settings.allow_degraded_start:
            raise
        logger.warning("LiveKit API unavailable, using degraded stub: %s", exc)
        app.state.lkapi = DegradedLiveKitAPI()

    try:
        if settings.gcp_credentials_b64:
            gcs_creds = json.loads(base64.b64decode(settings.gcp_credentials_b64).decode())
            app.state.gcs_client = gcs.Client.from_service_account_info(gcs_creds)
        else:
            app.state.gcs_client = gcs.Client()
    except Exception as exc:  # noqa: BLE001
        if not settings.allow_degraded_start:
            raise
        logger.warning("GCS client unavailable, using degraded stub: %s", exc)
        app.state.gcs_client = DegradedGCSClient(settings.gcs_bucket)

    sweep_interval = max(60, settings.idle_session_timeout_seconds // 4)
    sweep_task = asyncio.create_task(_sweep_idle_sessions(app, sweep_interval))

    yield

    sweep_task.cancel()
    try:
        await sweep_task
    except asyncio.CancelledError:
        pass
    await app.state.lkapi.aclose()


def create_app() -> FastAPI:
    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

    app = FastAPI(
        title="RemoteSupportLiveKit backend",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(request, exc: HTTPException):
        # Keep the contract shape: { "error": { "code", "message", "details" } }
        if isinstance(exc.detail, dict) and "error" in exc.detail:
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": getattr(exc, "code", "error"),
                    "message": str(exc.detail),
                    "details": {},
                }
            },
        )

    from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
    from starlette.requests import Request

    class _LatencyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
            from time import perf_counter

            from .metrics import REQUEST_LATENCY

            start = perf_counter()
            response = await call_next(request)
            duration = perf_counter() - start
            REQUEST_LATENCY.labels(
                method=request.method,
                path=request.url.path,
            ).observe(duration)
            return response

    app.add_middleware(_LatencyMiddleware)

    app.include_router(sessions.router)
    app.include_router(agent.router)
    app.include_router(transcripts.router)
    app.include_router(recordings.router)
    app.include_router(webhooks.router)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(
            content=generate_latest(),
            media_type=CONTENT_TYPE_LATEST,
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, object]:
        from .metrics import READYZ_DEPENDENCY_HEALTHY

        checks: dict[str, str] = {}

        try:
            from sqlalchemy import text

            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
            READYZ_DEPENDENCY_HEALTHY.labels(name="postgres").set(1)
        except Exception as exc:  # noqa: BLE001
            checks["postgres"] = f"error: {exc}"
            READYZ_DEPENDENCY_HEALTHY.labels(name="postgres").set(0)

        try:
            await app.state.lkapi.room.list_rooms(api.ListRoomsRequest())
            checks["livekit"] = "ok"
            READYZ_DEPENDENCY_HEALTHY.labels(name="livekit").set(1)
        except Exception as exc:  # noqa: BLE001
            checks["livekit"] = f"error: {exc}"
            READYZ_DEPENDENCY_HEALTHY.labels(name="livekit").set(0)

        try:
            # list_blobs needs object-list permission; bucket.exists() needs
            # storage.buckets.get, which the LiveKit Egress service account may
            # not have even when it can read/write objects.
            list(app.state.gcs_client.bucket(get_settings().gcs_bucket).list_blobs(max_results=1))
            checks["gcs"] = "ok"
            READYZ_DEPENDENCY_HEALTHY.labels(name="gcs").set(1)
        except Exception as exc:  # noqa: BLE001
            checks["gcs"] = f"error: {exc}"
            READYZ_DEPENDENCY_HEALTHY.labels(name="gcs").set(0)

        healthy = all(v == "ok" for v in checks.values())
        return {"status": "healthy" if healthy else "unhealthy", "checks": checks}

    return app


app = create_app()
