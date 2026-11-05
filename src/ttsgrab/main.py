"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ttsgrab import __version__
from ttsgrab.buildinfo import build_info
from ttsgrab.core.context import build_context
from ttsgrab.core.errors import ErrorBody, ErrorCode, ErrorEnvelope, GatewayError
from ttsgrab.core.obs import (
    HTTP_LATENCY,
    HTTP_REQUESTS,
    configure_logging,
    metrics_payload,
    new_request_id,
    request_id_var,
)
from ttsgrab.settings import Settings, load_settings

WEB_DIST = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging

    log = logging.getLogger("ttsgrab")
    settings: Settings = app.state.settings
    configure_logging(settings.log_level, settings.log_format)
    ctx = await build_context(settings)
    app.state.ctx = ctx

    from ttsgrab.workers.monitor import WorkerMonitor

    monitor = WorkerMonitor(ctx)
    await monitor.load_workers()
    ctx.extras["worker_monitor"] = monitor
    monitor.start()

    # Starlette does not run the lifespan of mounted sub-apps, and the MCP transport needs
    # its own task group. Enter it here so /mcp works.
    mcp_inner = getattr(app.state, "mcp_inner", None)
    # The MCP manager enters an anyio task group. anyio requires the exit to happen in the
    # same task as the enter, which manual __aenter__/__aexit__ cannot guarantee once the
    # lifespan is suspended across requests — so run it in its own background task.
    mcp_task: asyncio.Task | None = None
    mcp_ready = asyncio.Event()
    mcp_stop = asyncio.Event()

    async def _mcp_lifespan() -> None:
        if mcp_inner is None:  # pragma: no cover - caller guarantees otherwise
            return
        try:
            async with mcp_inner.router.lifespan_context(mcp_inner):
                mcp_ready.set()
                await mcp_stop.wait()
        except Exception:
            log.exception("MCP lifespan crashed")
            mcp_ready.set()

    if mcp_inner is not None:
        mcp_task = asyncio.create_task(_mcp_lifespan(), name="mcp-lifespan")
        try:
            await asyncio.wait_for(mcp_ready.wait(), timeout=10)
        except TimeoutError:
            log.warning("MCP lifespan did not start within 10s; /mcp may be unavailable")

    log.info(
        "ttsgrab %s ready — providers=%s voices=%d db=%s",
        build_info().label(),
        [p.name for p in ctx.registry.enabled()],
        len(ctx.registry.catalogue()),
        settings.database_url.split("///")[-1],
    )
    try:
        yield
    finally:
        if mcp_task is not None:
            mcp_stop.set()
            try:
                await asyncio.wait_for(mcp_task, timeout=10)
            except TimeoutError:
                mcp_task.cancel()
                # Await the cancellation so the task is really gone before the database
                # engine is disposed underneath it.
                with contextlib.suppress(asyncio.CancelledError):
                    await mcp_task
            except Exception:
                log.exception("MCP lifespan failed to stop cleanly")
        m = ctx.extras.get("worker_monitor")
        if m is not None:
            await m.stop()
        await ctx.registry.shutdown_all()
        from ttsgrab.db.session import dispose

        await dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(
        title="ttsgrab",
        version=__version__,
        description=(
            "Unified text-to-speech gateway. One API over many providers, with a web UI "
            "for humans and an MCP server for agents."
        ),
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )
    app.state.settings = settings

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[
                "X-TTSGrab-Provider",
                "X-TTSGrab-Voice",
                "X-TTSGrab-Cached",
                "X-TTSGrab-Cost-MicroUSD",
                "X-TTSGrab-Latency-Ms",
                "X-TTSGrab-History-Id",
                "X-TTSGrab-Unsupported",
                "X-TTSGrab-Fallback",
            ],
        )

    @app.middleware("http")
    async def _observability(request: Request, call_next):
        # Reject an oversized body before it is read. Starlette would happily buffer
        # whatever arrives, so without this `max_request_body_bytes` is a setting that
        # looks enforced and is not.
        declared = request.headers.get("content-length")
        limit = settings.security.max_request_body_bytes
        if declared and declared.isdigit() and int(declared) > limit:
            from ttsgrab.core.errors import RequestTooLarge

            return JSONResponse(
                status_code=413,
                content=ErrorEnvelope(
                    error=RequestTooLarge(
                        f"request body is {declared} bytes; limit is {limit}",
                        details={"limit_bytes": limit, "received_bytes": int(declared)},
                    ).to_body(new_request_id())
                ).model_dump(mode="json"),
            )

        rid = request.headers.get("x-request-id") or new_request_id()
        request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            HTTP_REQUESTS.labels(request.method, _route_label(request), "500").inc()
            raise
        elapsed = time.perf_counter() - started
        route = _route_label(request)
        HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
        HTTP_LATENCY.labels(request.method, route).observe(elapsed)
        response.headers["X-Request-Id"] = rid
        return response

    # -- error handling -----------------------------------------------------------------
    @app.exception_handler(GatewayError)
    async def _gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        body = exc.to_body(request_id_var.get())
        return JSONResponse(
            status_code=exc.http_status,
            content=ErrorEnvelope(error=body).model_dump(mode="json"),
            headers={"X-Request-Id": request_id_var.get()},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "param": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
                "message": err.get("msg", "invalid"),
                "type": err.get("type", "value_error"),
            }
            for err in exc.errors()
        ]
        body = ErrorBody(
            code=ErrorCode.INVALID_REQUEST,
            message="Request validation failed",
            retryable=False,
            details={"errors": errors},
            request_id=request_id_var.get(),
        )
        return JSONResponse(
            status_code=422,
            content=ErrorEnvelope(error=body).model_dump(mode="json"),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        import logging

        logging.getLogger("ttsgrab").exception("unhandled error")
        body = ErrorBody(
            code=ErrorCode.INTERNAL,
            message=f"{type(exc).__name__}: {exc}",
            retryable=True,
            request_id=request_id_var.get(),
        )
        return JSONResponse(
            status_code=500, content=ErrorEnvelope(error=body).model_dump(mode="json")
        )

    if settings.mcp.enabled:
        app.add_middleware(_McpPathNormalizer, path=settings.mcp.path)

    # -- routers ---------------------------------------------------------------------------
    from ttsgrab.api import docs as docs_api
    from ttsgrab.api.v1 import catalogue, data, jobs, keys, openai_compat, tts, workers

    app.include_router(tts.router)
    app.include_router(catalogue.router)
    app.include_router(data.router)
    app.include_router(workers.router)
    app.include_router(keys.router)
    app.include_router(jobs.router)
    app.include_router(openai_compat.router)
    app.include_router(docs_api.router)

    # -- MCP (optional extra) --------------------------------------------------------------
    # Mounted only when the `mcp` extra is installed; a missing optional dependency must
    # never stop the gateway from booting.
    mcp_inner = None
    settings.mcp.path.rstrip("/") if settings.mcp.enabled else None
    if settings.mcp.enabled:
        try:
            from ttsgrab import mcp_server

            mcp_inner = mcp_server.mount(app, settings.mcp.path)
        except SystemExit:
            logging.getLogger("ttsgrab").warning(
                "MCP is enabled in config but the `mcp` extra is not installed: "
                'pip install "ttsgrab[mcp]"'
            )
        except Exception:
            logging.getLogger("ttsgrab").exception("failed to mount the MCP server")

    @app.get("/healthz", tags=["ops"], summary="Liveness")
    async def healthz() -> dict[str, Any]:
        # Kept flat and additive: existing callers read `version`, and adding `build`
        # alongside it does not break them.
        return {"status": "ok", **build_info().as_dict()}

    @app.get("/readyz", tags=["ops"], summary="Readiness (checks DB and providers)")
    async def readyz(request: Request) -> Response:
        ctx = getattr(request.app.state, "ctx", None)
        if ctx is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        checks: dict[str, Any] = {}
        ok = True
        try:
            from sqlalchemy import text

            from ttsgrab.db.session import session_scope

            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = f"error: {exc}"
            ok = False
        enabled = [p.name for p in ctx.registry.enabled()]
        checks["providers_enabled"] = enabled
        if not enabled:
            checks["providers"] = "no provider enabled"
            ok = False
        else:
            checks["providers"] = "ok"
        checks["voices"] = len(ctx.registry.catalogue())
        return JSONResponse(
            {"status": "ready" if ok else "degraded", "checks": checks},
            status_code=200 if ok else 503,
        )

    @app.get("/metrics", tags=["ops"], summary="Prometheus metrics")
    async def metrics() -> Response:
        payload, content_type = metrics_payload()
        return Response(content=payload, media_type=content_type)

    # -- web UI ------------------------------------------------------------------------------
    _mount_web(app)
    app.state.mcp_inner = mcp_inner
    return app


class _McpPathNormalizer:
    """Point ``POST /mcp`` at ``/mcp/``.

    Starlette's ``Mount`` only issues a redirect for a trailing-slash mismatch on safe
    methods, so a POST to the bare mount path falls through to the SPA catch-all and 405s.
    A pure-ASGI middleware can rewrite the scope; ``BaseHTTPMiddleware`` cannot, because
    ``call_next`` keeps using the original scope.
    """

    def __init__(self, app, path: str) -> None:
        self.app = app
        self.path = path.rstrip("/")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == self.path and self.path:
            scope = dict(scope)
            scope["path"] = self.path + "/"
            scope["raw_path"] = scope["path"].encode()
        await self.app(scope, receive, send)


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    return getattr(route, "path", request.url.path)


def _mount_web(app: FastAPI) -> None:
    """Serve the built SPA if present; otherwise show a helpful placeholder."""
    dist = WEB_DIST
    if not dist.is_dir() or not (dist / "index.html").is_file():

        @app.get("/", include_in_schema=False)
        async def _no_ui() -> Response:
            return Response(
                "<h1>ttsgrab</h1><p>The web UI has not been built yet.</p>"
                "<p>Run <code>make web</code> (or <code>cd web &amp;&amp; npm run build</code>), "
                "then restart. API docs are at <a href='/docs'>/docs</a>.</p>",
                media_type="text/html",
            )

        return

    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    async def _index() -> Response:
        return Response((dist / "index.html").read_text(encoding="utf-8"), media_type="text/html")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str) -> Response:
        # Never swallow API routes.
        if full_path.startswith(
            (
                "v1/",
                "mcp",
                "docs",
                "redoc",
                "openapi.json",
                "metrics",
                "healthz",
                "readyz",
                "assets/",
            )
        ):
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        candidate = dist / full_path
        if candidate.is_file():
            return Response(candidate.read_bytes(), media_type=_guess_type(full_path))
        return Response((dist / "index.html").read_text(encoding="utf-8"), media_type="text/html")


def _guess_type(path: str) -> str:
    import mimetypes

    return mimetypes.guess_type(path)[0] or "application/octet-stream"


app = create_app()
