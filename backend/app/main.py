"""Application factory and public process health endpoint."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .api.auth import router as auth_router
from .api.auth import user_router
from .api.catalog import router as catalog_router
from .api.dashboard import router as dashboard_router
from .api.exports import router as exports_router
from .api.imports import router as imports_router
from .api.mail import router as mail_router
from .api.reviews import router as reviews_router
from .api.risk import router as risk_router
from .api.system import router as system_router
from .benchmark.scheduler import BenchmarkSyncScheduler
from .config import get_settings
from .db.session import create_engine
from .system.scheduler import MailSyncScheduler


def create_app() -> FastAPI:
    """Create the FastAPI application with its minimal public endpoint."""

    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        mail_scheduler = MailSyncScheduler(_app.state.db_engine, settings)
        benchmark_scheduler = BenchmarkSyncScheduler(_app.state.db_engine, settings)
        mail_task = asyncio.create_task(mail_scheduler.run())
        benchmark_task = asyncio.create_task(benchmark_scheduler.run())
        yield
        mail_task.cancel()
        benchmark_task.cancel()
        for task in (mail_task, benchmark_task):
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title=settings.service_name, lifespan=lifespan)
    app.state.settings = settings
    app.state.db_engine = create_engine(settings.database_url)

    @app.middleware("http")
    async def private_api_responses(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            # Product grants can change between requests, including for an
            # already logged-in account. Never reuse an authorized response.
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Vary"] = "Cookie, Origin"
        return response

    app.include_router(auth_router)
    app.include_router(user_router)
    app.include_router(imports_router)
    app.include_router(exports_router)
    app.include_router(mail_router)
    app.include_router(dashboard_router)
    app.include_router(reviews_router)
    app.include_router(catalog_router)
    app.include_router(risk_router)
    app.include_router(system_router)

    # CORS: the web dashboard is same-origin (served by Caddy alongside /api),
    # so browser requests never hit CORS preflight. The Tauri desktop client,
    # however, runs under http(s)://tauri.localhost and needs explicit headers
    # to reach the API cross-origin. Only the configured origins are allowed;
    # credentials stay enabled so the fund_session cookie works in both modes.
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Cookie", "x-async-sync"],
        )

    @app.get("/health/live", tags=["health"])
    def health_live() -> dict[str, str]:
        """Return a stable, non-sensitive liveness response."""

        return {
            "status": "ok",
            "service": settings.service_name,
        }

    return app


app = create_app()
