"""外部服务出站 HTTP 基础设施：统一重试策略 + JSON 传输。

模块划分：
- ``retry``：RetryPolicy / call_with_retry / Settings 映射
- ``httpx_json``：LiteLLM 等 httpx 调用
- ``requests_json``：MinerU 等 requests.Session 调用
"""
from app.integrations.http.retry import (
    PermanentHttpError,
    RetryableError,
    RetryPolicy,
    call_with_retry,
    retry_policy_from_settings,
)

__all__ = [
    "PermanentHttpError",
    "RetryableError",
    "RetryPolicy",
    "call_with_retry",
    "retry_policy_from_settings",
]
