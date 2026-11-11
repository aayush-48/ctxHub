"""ASGI application: REST API, login pages, MCP endpoint and web viewer in one service."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import api
from .auth import routes as auth_routes
from .auth.provider import provider
from .config import get_settings
from .db import dispose_engine, get_engine, init_engine
from .errors import CtxError
from .mcp_server import build_mcp_app
from .runtime import get_runtime
from .telemetry import instrument_engine, setup_logging, setup_tracing

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).parent / "static" / "app"


def create_app() -> FastAPI:
    settings = get_settings()
    setup_logging(settings)
    _mcp, mcp_app = build_mcp_app(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.run_migrations_on_start:
            from .migrate import upgrade_head

            await asyncio.to_thread(upgrade_head, settings.database_url)
        instrument_engine(settings, init_engine())
        get_runtime()
        await provider.ensure_web_client()
        async with mcp_app.router.lifespan_context(mcp_app):
            yield
        await dispose_engine()

    app = FastAPI(
        title="ctxHub API",
        version="0.1.0",
        description="Git-style version control for LLM conversation task state.",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    if settings.allowed_cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.allowed_cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.exception_handler(CtxError)
    async def ctx_error(_: Request, e: CtxError) -> JSONResponse:
        return JSONResponse(e.to_dict(), status_code=e.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, e: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(p) for p in err["loc"][1:]) or "body", "error": err["msg"]}
            for err in e.errors()
        ]
        return JSONResponse(
            {"error": "validation_failed", "message": "invalid request", "details": details},
            status_code=422,
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next) -> Response:
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith("/mcp"):
            resp.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith(("/app", "/login", "/consent", "/auth")):
            resp.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                # No form-action: browsers apply it to the consent redirect, which goes to
                # arbitrary client redirect URIs (including http://127.0.0.1 for the CLI).
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
        if settings.public_base_url.startswith("https://"):
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    # Cloud Run probes use /healthz; Google's front end reserves that path publicly, so
    # external checks use /health.
    @app.get("/health", include_in_schema=False)
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        async with get_engine().connect() as c:
            await c.execute(text("SELECT 1"))
        return {"status": "ok"}

    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    async def protected_resource_root() -> JSONResponse:
        # Some clients look for the metadata without the /mcp path suffix.
        base = settings.public_base_url.rstrip("/")
        return JSONResponse(
            {
                "resource": f"{base}/mcp",
                "authorization_servers": [base],
                "scopes_supported": ["ctx"],
                "bearer_methods_supported": ["header"],
            }
        )

    app.include_router(api.router)
    app.include_router(auth_routes.router)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/app/")

    if WEB_DIR.exists():
        app.mount("/app/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

    @app.get("/app/{path:path}", include_in_schema=False)
    async def web(path: str) -> Response:
        index = WEB_DIR / "index.html"
        if not index.exists():
            return JSONResponse({"message": "web viewer not built"}, status_code=404)
        candidate = (WEB_DIR / path).resolve()
        if path and candidate.is_file() and WEB_DIR.resolve() in candidate.parents:
            return FileResponse(candidate)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    setup_tracing(settings, app)
    # The MCP app owns /mcp, the OAuth endpoints and their metadata. Mounted last so the
    # routes above take precedence.
    app.mount("/", mcp_app)
    return app


app = create_app()
