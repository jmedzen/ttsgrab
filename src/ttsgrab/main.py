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

    # Retention: without this nothing ever deletes a history row or an audio file, so the
    # data directory grows with every synthesis until the volume fills.
    retention_task: asyncio.Task | None = None
    if settings.retention.enabled:
        from ttsgrab.core import retention

        retention_task = asyncio.create_task(
            retention.background_loop(settings, interval_s=settings.retention.interval_s),
            name="retention",
        )

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
        if retention_task is not None:
            retention_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retention_task
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
            headers=exc.http_headers(request_id_var.get()),
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
    from ttsgrab.api.v1 import (
        catalogue,
        data,
        favourites,
        jobs,
        keys,
        openai_compat,
        tts,
        workers,
    )
    from ttsgrab.api.v1 import (
        settings as settings_api,
    )

    app.include_router(tts.router)
    app.include_router(catalogue.router)
    app.include_router(data.router)
    app.include_router(workers.router)
    app.include_router(keys.router)
    app.include_router(favourites.router)
    app.include_router(settings_api.router)
    app.include_router(jobs.router)
    app.include_router(openai_compat.router)
    app.include_router(docs_api.router)

    # -- MCP (optional extra) --------------------------------------------------------------
    # Mounted only when the `mcp` extra is installed; a missing optional dependency must
    # never stop the gateway from booting.
    mcp_inner = None
    if settings.mcp.enabled:
        try:
            from ttsgrab import mcp_server

            # MCP tools synthesise speech and read usage — they are not read-only. The SDK
            # serves the whole tool surface behind the mount, and before this the mount had
            # no gate of its own: an unauthenticated caller got a session and could bill the
            # operator's provider accounts, bypassing every per-key control (rate limit,
            # allowlist, quota) simply by not having a key.
            #
            # Requiring a token here is the only place that can cover every tool, including
            # ones added to the SDK later.
            if not (settings.mcp.token or settings.security.admin_token):
                logging.getLogger("ttsgrab").warning(
                    "MCP is enabled but no TTSGRAB_MCP_TOKEN or admin token is configured; "
                    "refusing to mount it, because its tools spend money."
                )
                settings.mcp.enabled = False
            else:
                mcp_inner = mcp_server.mount(app, settings.mcp.path)
        except SystemExit:
            logging.getLogger("ttsgrab").warning(
                "MCP is enabled in config but the `mcp` extra is not installed: "
                'pip install "ttsgrab[mcp]"'
            )
        except Exception:
            logging.getLogger("ttsgrab").exception("failed to mount the MCP server")

    @app.api_route(
        "/healthz",
        tags=["ops"],
        summary="Liveness",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
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


_API_PREFIXES = (
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


def _mount_web(app: FastAPI) -> None:
    """Serve the built SPA if present; otherwise show a helpful placeholder."""
    dist = WEB_DIST
    if not dist.is_dir() or not (dist / "index.html").is_file():

        def _placeholder() -> Response:
            return Response(
                "<h1>ttsgrab</h1><p>The web UI has not been built yet.</p>"
                "<p>Run <code>make web</code> (or <code>cd web &amp;&amp; npm run build</code>), "
                "then restart. API docs are at <a href='/docs'>/docs</a>.</p>",
                media_type="text/html",
                # Same rule as the real shell: never cache. A browser that cached this
                # keeps showing "the UI has not been built" after the operator has built
                # it, which reads as a failed build.
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )

        @app.api_route("/", include_in_schema=False, methods=["GET", "HEAD"])
        async def _no_ui() -> Response:
            return _placeholder()

        # Deep links too, not just `/`. Someone who bookmarks /settings and then runs a
        # source checkout without `make web` gets a 404 that says nothing useful; the
        # placeholder at least tells them the one command that fixes it. API paths are still
        # 404 — they must not be answered with HTML.
        @app.api_route("/{full_path:path}", include_in_schema=False, methods=["GET", "HEAD"])
        async def _no_ui_deep(full_path: str) -> Response:
            if full_path.startswith(_API_PREFIXES):
                return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
            return _placeholder()

        return

    assets = dist / "assets"
    if assets.is_dir():

        class _HashedAssets(StaticFiles):
            """Every file here has a content hash in its name, so it can live in the
            browser cache indefinitely — a changed file arrives under a new name.

            StaticFiles sends no Cache-Control by default, which makes the browser guess.
            Guessing wrong in the permissive direction is how a rebuilt bundle keeps
            serving stale JS after a deploy.
            """

            async def get_response(self, path, scope):
                response = await super().get_response(path, scope)
                if response.status_code == 200:
                    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
                return response

        app.mount("/assets", _HashedAssets(directory=assets), name="assets")

    def _html(text: str, request: Request | None = None) -> Response:
        """index.html must never be cached.

        Its whole job is to name the content-hashed bundle. A cached copy keeps pointing at
        the previous build's filename, so an upgraded deployment serves new JS that the
        browser never asks for — the user sees an unchanged UI and concludes the feature
        does not exist. `no-cache` means revalidate (cheap: an ETag 304), not "do not
        store", which is what we want.
        """
        # A weak ETag over the bytes: `no-cache` forces revalidation on every load, and
        # this is what makes that revalidation a 304 instead of a full re-download.
        #
        # Starlette's Response does not evaluate If-None-Match itself, so the comparison is
        # explicit. Skipping it would make `no-cache` mean "download the whole page every
        # time" — correct but wasteful.
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        etag = f'W/"{digest}"'
        if request is not None:
            inm = request.headers.get("if-none-match", "")
            if etag in inm or inm.strip() == "*":
                return Response(
                    status_code=304,
                    headers={
                        "Cache-Control": "no-cache, must-revalidate",
                        "ETag": etag,
                        "X-TTSGrab-Build": build_info().label(),
                    },
                )
        return Response(
            text,
            media_type="text/html",
            headers={
                "Cache-Control": "no-cache, must-revalidate",
                "ETag": etag,
                # No content hash in the filename, so the build is what changes between
                # deploys — handy when someone reports "I still see the old UI".
                "X-TTSGRab-Build": build_info().label(),
            },
        )

    # HEAD alongside GET: FastAPI does not add it automatically, and uptime monitors and
    # load-balancer health checks probe with HEAD. A 405 there looks like a dead service.
    @app.api_route("/", include_in_schema=False, methods=["GET", "HEAD"])
    async def _index(request: Request) -> Response:
        return _html((dist / "index.html").read_text(encoding="utf-8"), request)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa(full_path: str, request: Request) -> Response:
        # Never swallow API routes.
        if full_path.startswith(_API_PREFIXES):
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        # Confine to the SPA directory before touching the filesystem.
        #
        # `full_path` is attacker-controlled and uvicorn has already percent-decoded it, so
        # `..%2f..%2f.env` arrives as a path with real separators — Starlette's `{path:path}`
        # converter cannot help, because by the time it runs the traversal is already part of
        # the path. Resolving and comparing is the only reliable check; `is_file()` alone
        # happily reads anything on disk.
        root = dist.resolve()
        try:
            candidate = (root / full_path).resolve()
        except (OSError, ValueError):
            return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
        # Outside the build directory is treated as "not a static file" and falls through to
        # the SPA shell — same as any unknown deep link. Returning 404 instead would tell a
        # prober which paths exist.
        served = candidate.is_relative_to(root) and candidate.is_file()
        if served:
            # Everything Vite emits under /assets carries a content hash in its name, so it
            # can be cached indefinitely; a changed file gets a new name.
            headers = (
                {"Cache-Control": "public, max-age=31536000, immutable"}
                if full_path.startswith("assets/")
                else {"Cache-Control": "no-cache"}
            )
            return Response(
                candidate.read_bytes(), media_type=_guess_type(full_path), headers=headers
            )
        return _html((dist / "index.html").read_text(encoding="utf-8"), request)


def _guess_type(path: str) -> str:
    import mimetypes

    return mimetypes.guess_type(path)[0] or "application/octet-stream"


app = create_app()
