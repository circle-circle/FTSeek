"""
Unified API response format — 统一错误响应规范 (2026-06-15)

所有 /api/* 路由使用此模块统一错误格式:
{
    "ok": true/false,
    "data": {...} | [...],     // 成功时返回的数据
    "error": {                  // 失败时的错误信息
        "code": "ERROR_CODE",
        "message": "人类可读描述",
        "retryable": true/false
    }
}

向后兼容: 旧路由仍可返回原始 dict, 新路由逐步迁移。
"""
from fastapi.responses import JSONResponse
from typing import Any, Optional


def api_ok(data: Any = None, status_code: int = 200) -> JSONResponse:
    """成功响应"""
    return JSONResponse(
        content={"ok": True, "data": data},
        status_code=status_code,
    )


def api_error(
    code: str,
    message: str,
    status_code: int = 500,
    retryable: bool = False,
    data: Any = None,
) -> JSONResponse:
    """错误响应 — 统一格式"""
    body = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        },
    }
    if data is not None:
        body["data"] = data
    return JSONResponse(content=body, status_code=status_code)


# ── 常用错误码 ──
def err_broker_unavailable() -> JSONResponse:
    return api_error("BROKER_UNAVAILABLE", "Futu连接不可用", 503, retryable=True)

def err_not_found(resource: str = "资源") -> JSONResponse:
    return api_error("NOT_FOUND", f"{resource}不存在", 404)

def err_bad_request(message: str) -> JSONResponse:
    return api_error("BAD_REQUEST", message, 400)

def err_rate_limited() -> JSONResponse:
    return api_error("RATE_LIMITED", "请求过于频繁", 429, retryable=True)

def err_unauthorized() -> JSONResponse:
    return api_error("UNAUTHORIZED", "未登录", 401)
