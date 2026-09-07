"""Application factory, middleware stack, lifespan, error handling."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.config import Settings, get_settings
from app.db import SessionLocal, engine, init_models
from app.http_status import HTTP_422_UNPROCESSABLE
from app.routers import admin, analytics, auth, decisions, kpis
from app.schemas import HealthOut
from app.security.headers import (
    BodySizeLimitMiddleware,
    HTTPSRedirectIfConfigured,
    SecurityHeadersMiddleware,
)

logging.basicConfig(
    level=logging.INFO, format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'
)
logger = logging.getLogger("edid")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    await init_models()
    if settings.bootstrap_admin_email and settings.bootstrap_admin_password:
        from app.seed import ensure_admin

        await ensure_admin(settings.bootstrap_admin_email, settings.bootstrap_admin_password)
    if settings.seed_demo_data and not settings.is_production:
        from app.seed import seed_demo

        await seed_demo()
    logger.info("startup environment=%s version=%s", settings.environment, __version__)
    yield
    await engine.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        # Interactive docs are useful in development and noise in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    # Order matters: outermost first.
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(HTTPSRedirectIfConfigured, enabled=settings.force_https)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.trusted_hosts))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_credentials=False,  # bearer tokens only; no cookie surface to forge
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        max_age=600,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    prefix = settings.api_prefix
    app.include_router(auth.router, prefix=prefix)
    app.include_router(decisions.router, prefix=prefix)
    app.include_router(analytics.router, prefix=prefix)
    app.include_router(kpis.router, prefix=prefix)
    app.include_router(admin.router, prefix=prefix)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Echo the field paths, never the submitted values (they may be secrets).
        fields = [
            {"loc": [str(part) for part in error.get("loc", [])], "msg": error.get("msg", "")}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=HTTP_422_UNPROCESSABLE,
            content={"detail": "Validation failed", "errors": fields},
        )

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "")
        logger.exception("unhandled error request_id=%s", request_id)
        # Generic body: stack traces and driver messages never reach the client.
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": "Internal server error", "request_id": request_id},
        )

    @app.get(f"{prefix}/health", response_model=HealthOut, tags=["meta"])
    async def health() -> HealthOut:
        database = "ok"
        try:
            async with SessionLocal() as session:
                await session.execute(text("SELECT 1"))
        except Exception:
            database = "unavailable"
        return HealthOut(
            status="ok" if database == "ok" else "degraded",
            version=__version__,
            environment=settings.environment,
            database=database,
        )

    if FRONTEND_DIR.is_dir():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(FRONTEND_DIR / "index.html")

    return app


app = create_app()
