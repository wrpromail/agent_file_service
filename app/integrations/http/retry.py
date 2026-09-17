"""出站 HTTP 可重试策略：LiteLLM / MinerU 等外部服务共用。"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

T = TypeVar("T")


class RetryableError(Exception):
    """可安全重试的临时性失败（超时、连接、429、5xx 等）。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        rate_limited: bool = False,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.rate_limited = rate_limited
        self.__cause__ = cause


class PermanentHttpError(Exception):
    """不应重试的 HTTP 错误（多数 4xx）。"""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str = "",
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.__cause__ = cause


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略。

    ``max_attempts``：总尝试次数（含首次）。1 = 不重试；3 = 最多 2 次重试。
    """

    max_attempts: int = 3
    backoff_base: float = 1.0
    backoff_max: float = 30.0
    rate_limit_wait: float = 65.0
    jitter_ratio: float = 0.3
    retry_status_codes: frozenset[int] = field(
        default_factory=lambda: frozenset({429, 500, 502, 503, 504})
    )

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            object.__setattr__(self, "max_attempts", 1)
        if self.backoff_base < 0:
            object.__setattr__(self, "backoff_base", 0.0)
        if self.backoff_max < 0:
            object.__setattr__(self, "backoff_max", 0.0)

    def is_retryable_status(self, status_code: int) -> bool:
        if status_code in self.retry_status_codes:
            return True
        return 500 <= status_code <= 599

    def sleep_seconds(self, attempt: int, *, rate_limited: bool = False) -> float:
        """``attempt`` 为已失败的次数（从 1 起）。"""
        if rate_limited:
            base = max(self.rate_limit_wait, 1.0)
        else:
            base = min(self.backoff_base * (2 ** max(attempt - 1, 0)), self.backoff_max)
        jitter = random.uniform(0, self.jitter_ratio * base) if base > 0 else 0.0
        return base + jitter


def call_with_retry(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """执行 ``fn``，对 :class:`RetryableError` 按策略退避重试。

    其它异常立即抛出。耗尽次数后抛出最后一次的 :class:`RetryableError`。
    """
    last_error: BaseException | None = None
    attempts = max(1, policy.max_attempts)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except RetryableError as exc:
            last_error = exc
            if attempt >= attempts:
                break
            wait = policy.sleep_seconds(attempt, rate_limited=exc.rate_limited)
            if on_retry is not None:
                on_retry(attempt, exc, wait)
            if wait > 0:
                sleep(wait)
        except PermanentHttpError:
            raise
    assert last_error is not None
    raise last_error


def retry_policy_from_settings(settings: object, *, service: str | None = None) -> RetryPolicy:
    """从 Settings 构建策略。

    - 全局：``outbound_http_*``
    - 服务覆盖：``{service}_max_attempts`` 等；值为 0 / 未设则继承全局
    """
    def _int(name: str, default: int) -> int:
        value = getattr(settings, name, default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _float(name: str, default: float) -> float:
        value = getattr(settings, name, default)
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    max_attempts = _int("outbound_http_max_attempts", 3)
    backoff_base = _float("outbound_http_backoff_base", 1.0)
    backoff_max = _float("outbound_http_backoff_max", 30.0)
    rate_limit_wait = _float("outbound_http_rate_limit_wait", 65.0)

    if service:
        svc_attempts = _int(f"{service}_max_attempts", 0)
        if svc_attempts > 0:
            max_attempts = svc_attempts
        svc_base = _float(f"{service}_backoff_base", 0.0)
        if svc_base > 0:
            backoff_base = svc_base
        svc_max = _float(f"{service}_backoff_max", 0.0)
        if svc_max > 0:
            backoff_max = svc_max
        svc_rl = _float(f"{service}_rate_limit_wait", 0.0)
        if svc_rl > 0:
            rate_limit_wait = svc_rl

    return RetryPolicy(
        max_attempts=max_attempts,
        backoff_base=backoff_base,
        backoff_max=backoff_max,
        rate_limit_wait=rate_limit_wait,
    )
