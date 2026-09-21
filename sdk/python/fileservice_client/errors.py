"""File Service SDK 异常层次。"""

from __future__ import annotations

from typing import Any


class FileServiceError(RuntimeError):
    """File Service SDK 基础异常。"""

    def __init__(self, message: str, status_code: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body or {}


class FileServiceAuthError(FileServiceError):
    """缺少或错误的 Bearer（HTTP 401）。"""


class FileServicePermissionError(FileServiceError):
    """租户不允许（HTTP 403）。"""


class FileServiceNotFoundError(FileServiceError):
    """引用不存在或不属于本租户（HTTP 404）。"""


class FileServiceValidationError(FileServiceError):
    """请求参数无效（HTTP 400 / 413）。"""


class FileServiceConflictError(FileServiceError):
    """资源冲突（HTTP 409）。"""


class FileServiceQuotaError(FileServiceError):
    """租户配额（HTTP 429）。"""


class FileServiceUnavailableError(FileServiceError):
    """服务不可用（HTTP 5xx）。"""


class FileServiceTimeoutError(FileServiceError):
    """等待解析超时（客户端），或网关 504。"""


class FileServiceParseError(FileServiceError):
    """解析失败（content.status=failed 或 message_code 表示失败）。"""


def error_message(body: dict[str, Any], fallback: str) -> str:
    """从 FastAPI 响应体提取可读错误信息。"""
    detail = body.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    if isinstance(detail, dict):
        msg = detail.get("message") or detail.get("detail")
        if msg:
            return str(msg)
    if isinstance(detail, list) and detail:
        parts = []
        for item in detail:
            if isinstance(item, dict):
                loc = ".".join(str(x) for x in item.get("loc", ()))
                msg = item.get("msg", "")
                parts.append(f"{loc}: {msg}" if loc else str(msg))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    if body.get("message"):
        return str(body["message"])
    if "error" in body:
        err = body["error"]
        if isinstance(err, str) and err:
            return err
    return fallback


def safe_json(resp) -> dict:
    try:
        result = resp.json()
        if isinstance(result, dict):
            return result
        return {"data": result}
    except (ValueError, TypeError):
        return {"_raw": (resp.text or "")[:500]}


def raise_for_status(resp, *, context: str = "request") -> None:
    """根据 HTTP 状态码抛出对应 SDK 异常。"""
    if resp.status_code < 400:
        return

    body = safe_json(resp)
    fallback = f"File Service 返回 HTTP {resp.status_code}"

    if resp.status_code in (400, 413, 422):
        raise FileServiceValidationError(
            error_message(body, fallback), status_code=resp.status_code, body=body
        )
    if resp.status_code == 401:
        raise FileServiceAuthError(
            error_message(body, "缺少或错误的 API Key"), status_code=401, body=body
        )
    if resp.status_code == 403:
        raise FileServicePermissionError(
            error_message(body, "租户不允许"), status_code=403, body=body
        )
    if resp.status_code == 404:
        raise FileServiceNotFoundError(
            error_message(body, "文件引用不存在"), status_code=404, body=body
        )
    if resp.status_code == 409:
        raise FileServiceConflictError(
            error_message(body, "资源冲突"), status_code=409, body=body
        )
    if resp.status_code == 429:
        raise FileServiceQuotaError(
            error_message(body, "租户配额已满"), status_code=429, body=body
        )
    if resp.status_code == 504:
        raise FileServiceTimeoutError(
            error_message(body, f"{context} 超时"), status_code=504, body=body
        )
    if resp.status_code >= 500:
        raise FileServiceUnavailableError(
            error_message(body, fallback), status_code=resp.status_code, body=body
        )
    raise FileServiceError(error_message(body, fallback), status_code=resp.status_code, body=body)
