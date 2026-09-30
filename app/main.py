import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.routes import auth_uc, convert, files, sessions
from app.bootstrap import ensure_runtime_dirs, ensure_schema
from app.config import get_settings
from app.core.dependency_health import (
    get_last_dependency_report,
    run_dependency_checks,
    run_startup_dependency_checks,
)
from app.core.security import ensure_api_keys
from app.uc.middleware import UcSessionMiddleware

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent.parent / "web"


async def _run_interval_task(name: str, interval_seconds: int, fn) -> None:
    """Run ``fn`` periodically in a worker thread; never exits the loop on errors."""
    await asyncio.sleep(min(10, max(1, interval_seconds)))
    while True:
        started = time.monotonic()
        try:
            result = await asyncio.to_thread(fn)
            logger.info("periodic %s completed result=%s", name, result)
        except Exception:
            logger.exception("periodic %s failed", name)
        elapsed = time.monotonic() - started
        delay = max(1.0, float(interval_seconds) - elapsed)
        await asyncio.sleep(delay)


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.environ.get("PYTEST_VERSION"):
        from app.log_config import configure_logging

        configure_logging()
        logger.info(
            "file-service starting role=%s",
            os.environ.get("SERVICE_ROLE") or "api",
        )
    settings = get_settings()
    ensure_runtime_dirs(settings)
    mcp_cm = None
    if not os.environ.get("PYTEST_VERSION"):
        try:
            from app.mcp_convert import mcp as mcp_server

            if mcp_server is not None:
                mcp_cm = mcp_server.session_manager.run()
                await mcp_cm.__aenter__()
        except Exception:
            logger.exception("MCP convert session manager failed to start")
            mcp_cm = None
    # pytest uses an in-memory engine via get_db; skip the process engine.
    if not os.environ.get("PYTEST_VERSION"):
        ensure_schema()
    # Authorization 必填且不可关闭；未配置 API_KEYS 时生成 ephemeral key 并打日志
    ensure_api_keys(settings)
    from app.uc.config import get_uc_config

    uc_cfg = get_uc_config()
    logger.info("UC gate enabled=%s mock=%s", int(uc_cfg.enabled), int(uc_cfg.mock))
    if uc_cfg.enabled and uc_cfg.ticket_secret == "uc-dev":
        logger.warning(
            "UC enabled but HMAC secret fell back to hardcoded uc-dev; "
            "set FILE_SERVICE_UC_SESSION_SECRET (or API_KEYS)"
        )
    # 启动时探测 LiteLLM 等外部依赖（strict 时失败会抛错，阻止就绪）
    await asyncio.to_thread(run_startup_dependency_checks, settings)

    tasks: list[asyncio.Task] = []
    if settings.gc_beat_enabled:
        from app.tasks.gc_tasks import expire_references_task, run_blob_gc_task

        tasks.append(
            asyncio.create_task(
                _run_interval_task(
                    "expire_references",
                    settings.gc_expire_interval_seconds,
                    expire_references_task,
                )
            )
        )
        tasks.append(
            asyncio.create_task(
                _run_interval_task(
                    "blob_gc",
                    settings.gc_blob_interval_seconds,
                    run_blob_gc_task,
                )
            )
        )
    yield
    if mcp_cm is not None:
        try:
            await mcp_cm.__aexit__(None, None, None)
        except Exception:
            logger.debug("MCP convert session manager shutdown failed", exc_info=True)
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


def _route_group(path: str) -> str:
    """Coarse route label for http_requests_total (avoid high-cardinality paths)."""
    if path.startswith("/v1/files"):
        return "v1_files"
    if path.startswith("/v1/sessions"):
        return "v1_sessions"
    if (
        path.startswith("/convert")
        or path.startswith("/extract-text")
        or path.startswith("/generate-excel")
        or path.startswith("/cache")
    ):
        return "convert_compat"
    if path.startswith("/mcp"):
        return "mcp"
    if path.startswith("/api/auth"):
        return "auth"
    if path == "/health" or path.startswith("/health"):
        return "health"
    if path == "/metrics":
        return "metrics"
    if path.startswith("/shared"):
        return "static"
    if path.endswith(".html") or path == "/":
        return "ui"
    return "other"


def _inc_http_metric(request: Request, status_code: int) -> None:
    try:
        from app.core.metrics import inc_metric

        status_class = f"{status_code // 100}xx" if status_code else "unknown"
        inc_metric(
            "http_requests_total",
            method=request.method.upper(),
            route_group=_route_group(request.url.path),
            status_class=status_class,
        )
    except Exception:
        logger.debug("http metrics increment failed", exc_info=True)


class HttpMetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        try:
            response = await call_next(request)
        except Exception:
            _inc_http_metric(request, 500)
            raise
        status_code = getattr(response, "status_code", 0) or 0
        _inc_http_metric(request, status_code)
        return response


# 生产暴露面：关闭 FastAPI 自带文档与 schema（/docs、/redoc、/openapi.json）
app = FastAPI(
    title=get_settings().app_name,
    version="0.1.0",
    lifespan=lifespan,
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
)
app.add_middleware(UcSessionMiddleware)
app.add_middleware(HttpMetricsMiddleware)
try:
    from app.mcp_convert import ConvertMcpAuthMiddleware, mcp as mcp_server

    app.add_middleware(ConvertMcpAuthMiddleware)
    if mcp_server is not None:
        app.mount("/mcp", mcp_server.streamable_http_app())
except Exception:
    logger.warning("MCP convert endpoint not mounted", exc_info=True)
app.include_router(auth_uc.router)
app.include_router(files.router)
app.include_router(sessions.router)
app.include_router(convert.router)


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus text metrics (no auth — scrape from private network only)."""
    from starlette.responses import PlainTextResponse

    from app.core.metrics import get_metrics

    return PlainTextResponse(get_metrics().render_prometheus(), media_type="text/plain; version=0.0.4")


@app.get("/health")
def health(deep: bool = False) -> dict[str, Any]:
    """进程存活 + 依赖状态。

    - 默认返回最近一次启动检查结果（不重复打外部服务），供 compose/k8s liveness 使用。
    - ``?deep=true`` 时重新探测依赖（较慢，用于排障）。
    - 进程自身正常时 HTTP 始终 200；依赖失败时 ``status=degraded``，``dependencies.ok=false``。
    """
    settings = get_settings()
    if deep:
        report = run_dependency_checks(settings)
    else:
        report = get_last_dependency_report()
        if report is None:
            # 未走过 lifespan 时：配置关闭则直接标记 skip，否则现场探测
            if settings.startup_dependency_check:
                report = run_dependency_checks(settings)
            else:
                from app.core.dependency_health import CheckResult, DependencyHealthReport

                report = DependencyHealthReport(
                    ok=True,
                    checked_at=0.0,
                    checks=[
                        CheckResult(
                            name="startup_dependency_check",
                            ok=True,
                            required=False,
                            skipped=True,
                            detail="disabled by config",
                        )
                    ],
                )

    status = "ok" if report.ok else "degraded"
    return {
        "status": status,
        "dependencies": report.to_dict(),
    }


def _mount_dev_console(application: FastAPI, web_dir: Path) -> None:
    """Serve skillmesh-style static MPA when enabled and ``web/`` is present (api image)."""
    settings = get_settings()
    if not settings.dev_console_enabled:
        logger.info("dev console skipped: DEV_CONSOLE_ENABLED=false")
        return
    if not web_dir.is_dir():
        logger.info("dev console skipped: web dir missing at %s", web_dir)
        return

    def _html_response(filename: str) -> FileResponse:
        return FileResponse(web_dir / filename)

    def _make_html_handler(filename: str):
        def _handler() -> FileResponse:
            return _html_response(filename)

        return _handler

    html_pages = (
        ("/", "index.html"),
        ("/auth", "auth.html"),
        ("/auth/", "auth.html"),
        ("/dashboard.html", "dashboard.html"),
        ("/files.html", "files.html"),
        ("/file.html", "file.html"),
        ("/upload.html", "upload.html"),
        ("/session.html", "session.html"),
        ("/metrics.html", "metrics.html"),
        ("/playground.html", "playground.html"),
    )
    for route, filename in html_pages:
        path = web_dir / filename
        if not path.is_file():
            logger.warning("dev console page missing: %s", path)
            continue
        application.add_api_route(
            route,
            _make_html_handler(filename),
            methods=["GET"],
            include_in_schema=False,
            response_class=FileResponse,
        )

    shared = web_dir / "shared"
    if shared.is_dir():
        application.mount("/shared", StaticFiles(directory=str(shared)), name="shared")
    logger.info("dev console mounted from %s", web_dir)


_mount_dev_console(app, _WEB_DIR)
