"""启动 / 探活：检查已配置的外部依赖（LiteLLM、Valkey、MinerU 等）是否可达。

设计要点：
- 仅检查「当前配置会用到」的依赖（例如 layout=mineru 才查 MinerU；celery 才查队列）。
- 默认不阻塞进程启动（strict=false）；结果写入日志，并暴露给 GET /health。
- LiteLLM 用轻量 /v1/models（或 liveliness），不做会真实打模型的 /health。
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# 最近一次检查结果（供 /health 读取，避免每次请求都打外部服务）
_last_report: DependencyHealthReport | None = None


@dataclass
class CheckResult:
    name: str
    ok: bool
    required: bool
    skipped: bool = False
    detail: str = ""
    latency_ms: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if not data["meta"]:
            data.pop("meta")
        if data["latency_ms"] is None:
            data.pop("latency_ms")
        return data


@dataclass
class DependencyHealthReport:
    ok: bool
    checked_at: float
    checks: list[CheckResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checked_at": self.checked_at,
            "checks": [c.to_dict() for c in self.checks],
        }


def get_last_dependency_report() -> DependencyHealthReport | None:
    return _last_report


def _set_last_report(report: DependencyHealthReport) -> None:
    global _last_report
    _last_report = report


def _timed() -> float:
    return time.perf_counter()


def check_database(settings: Settings, *, timeout: float) -> CheckResult:
    name = "database"
    start = _timed()
    try:
        from sqlalchemy import text

        from app.db.session import engine

        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return CheckResult(
            name=name,
            ok=True,
            required=True,
            detail="SELECT 1 ok",
            latency_ms=round((_timed() - start) * 1000, 1),
            meta={"backend": "sqlite" if settings.is_sqlite else "sql"},
        )
    except Exception as exc:  # noqa: BLE001 — 汇总为检查结果
        return CheckResult(
            name=name,
            ok=False,
            required=True,
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=round((_timed() - start) * 1000, 1),
        )


def check_litellm(settings: Settings, *, timeout: float) -> CheckResult:
    """探测 LiteLLM 代理：连通 + 鉴权 +（尽量）确认配置的模型在列表中。"""
    name = "litellm"
    base = (settings.litellm_base_url or "").strip().rstrip("/")
    if not base:
        return CheckResult(
            name=name,
            ok=False,
            required=True,
            detail="litellm_base_url is empty",
        )

    headers: dict[str, str] = {}
    if settings.litellm_api_key:
        headers["Authorization"] = f"Bearer {settings.litellm_api_key}"

    wanted = {
        m
        for m in (
            (settings.vision_model or "").strip(),
            (settings.audio_model or "").strip(),
            (settings.video_model or "").strip(),
        )
        if m
    }

    start = _timed()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            # 1) 优先 liveliness（不打模型、不耗额度）
            live = client.get(f"{base}/health/liveliness", headers=headers)
            live_ok = live.status_code < 500

            # 2) /v1/models：验证鉴权与模型目录
            models_resp = client.get(f"{base}/v1/models", headers=headers)
            latency = round((_timed() - start) * 1000, 1)

            if models_resp.status_code in {401, 403}:
                return CheckResult(
                    name=name,
                    ok=False,
                    required=True,
                    detail=f"auth failed HTTP {models_resp.status_code} (check LITELLM_API_KEY)",
                    latency_ms=latency,
                    meta={"base_url": base},
                )

            if models_resp.status_code >= 400:
                # 部分代理无 /v1/models：退化为 liveliness / 根路径可达
                if live_ok and live.status_code < 400:
                    return CheckResult(
                        name=name,
                        ok=True,
                        required=True,
                        detail=(
                            f"reachable (liveliness HTTP {live.status_code}); "
                            f"/v1/models HTTP {models_resp.status_code}"
                        ),
                        latency_ms=latency,
                        meta={"base_url": base, "models_checked": False},
                    )
                return CheckResult(
                    name=name,
                    ok=False,
                    required=True,
                    detail=f"/v1/models HTTP {models_resp.status_code}",
                    latency_ms=latency,
                    meta={"base_url": base},
                )

            available: set[str] = set()
            try:
                body = models_resp.json()
                for item in body.get("data") or []:
                    mid = item.get("id") if isinstance(item, dict) else None
                    if mid:
                        available.add(str(mid))
            except Exception:  # noqa: BLE001
                available = set()

            missing = sorted(wanted - available) if available else []
            if available and missing:
                return CheckResult(
                    name=name,
                    ok=False,
                    required=True,
                    detail=f"reachable but models missing from /v1/models: {', '.join(missing)}",
                    latency_ms=latency,
                    meta={
                        "base_url": base,
                        "wanted": sorted(wanted),
                        "missing": missing,
                        "model_count": len(available),
                    },
                )

            detail = f"ok HTTP {models_resp.status_code}"
            if available and wanted:
                detail += f"; models present: {', '.join(sorted(wanted))}"
            elif not available:
                detail += "; model list empty/unparsed (connectivity only)"

            return CheckResult(
                name=name,
                ok=True,
                required=True,
                detail=detail,
                latency_ms=latency,
                meta={
                    "base_url": base,
                    "wanted": sorted(wanted),
                    "model_count": len(available),
                },
            )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name=name,
            ok=False,
            required=True,
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=round((_timed() - start) * 1000, 1),
            meta={"base_url": base},
        )


def check_valkey(settings: Settings, *, timeout: float) -> CheckResult:
    """Celery 后端时检查 broker/result 用的 Redis/Valkey。"""
    name = "valkey"
    backend = (settings.parse_task_backend or "local").strip().lower()
    if backend != "celery" and not settings.parse_sync:
        # local 后台任务不依赖队列；仍可能只开 parse_sync
        pass
    if backend != "celery":
        return CheckResult(
            name=name,
            ok=True,
            required=False,
            skipped=True,
            detail="skipped (parse_task_backend != celery)",
        )

    url = (settings.celery_broker_url or settings.valkey_url or "").strip()
    if not url:
        return CheckResult(
            name=name,
            ok=False,
            required=True,
            detail="celery_broker_url / valkey_url empty",
        )

    start = _timed()
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=timeout, socket_timeout=timeout)
        try:
            pong = client.ping()
        finally:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        if not pong:
            return CheckResult(
                name=name,
                ok=False,
                required=True,
                detail="PING returned falsy",
                latency_ms=round((_timed() - start) * 1000, 1),
                meta={"url": _redact_url(url)},
            )
        return CheckResult(
            name=name,
            ok=True,
            required=True,
            detail="PING ok",
            latency_ms=round((_timed() - start) * 1000, 1),
            meta={"url": _redact_url(url)},
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name=name,
            ok=False,
            required=True,
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=round((_timed() - start) * 1000, 1),
            meta={"url": _redact_url(url)},
        )


def check_mineru(settings: Settings, *, timeout: float) -> CheckResult:
    name = "mineru"
    layout = (settings.layout_parser_name or "").strip().lower()
    if layout != "mineru":
        return CheckResult(
            name=name,
            ok=True,
            required=False,
            skipped=True,
            detail="skipped (layout_parser_name != mineru)",
        )

    base = (settings.mineru_base_url or "").strip().rstrip("/")
    if not base:
        return CheckResult(
            name=name,
            ok=False,
            required=True,
            detail="mineru_base_url empty",
        )
    if not (settings.mineru_api_token or "").strip():
        return CheckResult(
            name=name,
            ok=False,
            required=True,
            detail="MINERU_API_TOKEN empty",
            meta={"base_url": base},
        )

    start = _timed()
    headers = {"Authorization": f"Bearer {settings.mineru_api_token}"}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            # MinerU 公开 API 无统一 liveliness；对 base 做 GET/HEAD，4xx 仍算可达
            resp = client.get(base, headers=headers)
            latency = round((_timed() - start) * 1000, 1)
            # 连接成功即 ok；5xx 视为不可用
            if resp.status_code >= 500:
                return CheckResult(
                    name=name,
                    ok=False,
                    required=True,
                    detail=f"HTTP {resp.status_code}",
                    latency_ms=latency,
                    meta={"base_url": base},
                )
            return CheckResult(
                name=name,
                ok=True,
                required=True,
                detail=f"reachable HTTP {resp.status_code}",
                latency_ms=latency,
                meta={"base_url": base},
            )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name=name,
            ok=False,
            required=True,
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=round((_timed() - start) * 1000, 1),
            meta={"base_url": base},
        )


def _redact_url(url: str) -> str:
    """隐藏 redis://user:pass@host 中的口令。"""
    try:
        parsed = urlparse(url)
        if parsed.password is None and "@" not in (parsed.netloc or ""):
            return url
        host = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        user = parsed.username or ""
        auth = f"{user}:***@" if user or parsed.password else ""
        return f"{parsed.scheme}://{auth}{host}{port}{parsed.path or ''}"
    except Exception:  # noqa: BLE001
        return "<redacted>"


def run_dependency_checks(
    settings: Settings | None = None,
    *,
    timeout: float | None = None,
) -> DependencyHealthReport:
    settings = settings or get_settings()
    timeout = (
        float(timeout)
        if timeout is not None
        else float(getattr(settings, "startup_dependency_check_timeout", 5) or 5)
    )

    checks = [
        check_database(settings, timeout=timeout),
        check_litellm(settings, timeout=timeout),
        check_valkey(settings, timeout=timeout),
        check_mineru(settings, timeout=timeout),
    ]

    # 仅 required 且未 skip 的失败会影响 ok
    ok = all(c.ok or c.skipped or not c.required for c in checks)
    report = DependencyHealthReport(ok=ok, checked_at=time.time(), checks=checks)
    _set_last_report(report)
    return report


def log_dependency_report(report: DependencyHealthReport) -> None:
    for c in report.checks:
        if c.skipped:
            logger.info("dependency check skip name=%s detail=%s", c.name, c.detail)
            continue
        level = logging.INFO if c.ok else logging.ERROR
        logger.log(
            level,
            "dependency check %s name=%s latency_ms=%s detail=%s",
            "ok" if c.ok else "FAIL",
            c.name,
            c.latency_ms,
            c.detail,
        )
    if report.ok:
        logger.info("startup dependency checks passed")
    else:
        failed = [c.name for c in report.checks if not c.ok and not c.skipped and c.required]
        logger.error("startup dependency checks failed: %s", ", ".join(failed))


def run_startup_dependency_checks(settings: Settings | None = None) -> DependencyHealthReport:
    """API lifespan 调用：跑检查、打日志；strict 时失败则抛错阻止启动。"""
    settings = settings or get_settings()
    enabled = bool(getattr(settings, "startup_dependency_check", True))
    if not enabled:
        report = DependencyHealthReport(
            ok=True,
            checked_at=time.time(),
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
        _set_last_report(report)
        logger.info("startup dependency checks disabled")
        return report

    report = run_dependency_checks(settings)
    log_dependency_report(report)

    strict = bool(getattr(settings, "startup_dependency_check_strict", False))
    if strict and not report.ok:
        failed = [
            f"{c.name}: {c.detail}"
            for c in report.checks
            if not c.ok and not c.skipped and c.required
        ]
        raise RuntimeError(
            "startup dependency check failed (strict mode): " + "; ".join(failed)
        )
    return report
