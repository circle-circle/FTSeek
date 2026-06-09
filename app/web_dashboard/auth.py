"""
Dashboard 登录认证中间件

功能:
  - 密码登录 (密码存储在 .env.us 的 DASHBOARD_PASSWORD)
  - Session cookie 认证 (HMAC-SHA256 签名)
  - 所有页面和 API 均需登录，仅 /login 和 /api/auth/* 豁免
  - 24小时自动过期
"""

import os
import hashlib
import hmac
import time
import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ftseek_us.auth")

ENV_FILE = Path(__file__).parent.parent.parent / ".env.us"
SESSION_MAX_AGE = 86400  # 24 hours


def _read_env(key: str, default: str = "") -> str:
    """从 .env.us 读取配置"""
    try:
        if ENV_FILE.exists():
            with open(ENV_FILE) as f:
                for line in f:
                    stripped = line.strip()
                    if stripped.startswith(f"{key}=") and not stripped.startswith("#"):
                        return stripped.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.getenv(key, default)


def get_password_md5() -> str:
    """获取登录密码 MD5 (从 .env.us 的 DASHBOARD_PASSWORD_MD5)"""
    return _read_env("DASHBOARD_PASSWORD_MD5", "")


def get_password_plain() -> str:
    """获取明文密码 (备选, 从 .env.us 的 DASHBOARD_PASSWORD)"""
    return _read_env("DASHBOARD_PASSWORD", "")


def has_password_configured() -> bool:
    """是否已配置登录密码"""
    return bool(get_password_md5() or get_password_plain())


def verify_password(input_password: str) -> bool:
    """
    验证用户输入的密码 (明文)

    优先用 MD5 比对 (DASHBOARD_PASSWORD_MD5),
    备选明文比对 (DASHBOARD_PASSWORD).
    未配置密码时允许登录 (首次使用).
    """
    expected_md5 = get_password_md5()
    expected_plain = get_password_plain()

    if not expected_md5 and not expected_plain:
        logger.warning("[Auth] DASHBOARD_PASSWORD_MD5 未设置，任何密码均可登录！请在 .env.us 中配置")
        return True

    # 优先 MD5 比对
    if expected_md5:
        input_md5 = hashlib.md5(input_password.encode()).hexdigest()
        if hmac.compare_digest(input_md5, expected_md5):
            return True

    # 备选明文比对
    if expected_plain:
        if hmac.compare_digest(input_password, expected_plain):
            return True

    return False


def get_secret_key() -> str:
    """获取签名密钥 (从 .env.us 的 DASHBOARD_SECRET, 无则用密码派生)"""
    secret = _read_env("DASHBOARD_SECRET", "")
    if not secret:
        pwd = get_password_md5() or get_password_plain()
        if pwd:
            secret = hashlib.sha256(f"ftseek-us-{pwd}".encode()).hexdigest()
    return secret or "ftseek-us-default-secret-change-me"


def create_session_token() -> str:
    """创建 session token (payload.timestamp + HMAC 签名)"""
    secret = get_secret_key()
    payload = json.dumps({"t": int(time.time())}, separators=(",", ":"))
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_session_token(token: str) -> bool:
    """验证 session token 是否有效且未过期"""
    if not token or "." not in token:
        return False
    try:
        payload_str, sig = token.rsplit(".", 1)
        secret = get_secret_key()
        expected_sig = hmac.new(secret.encode(), payload_str.encode(), hashlib.sha256).hexdigest()[:32]

        if not hmac.compare_digest(sig, expected_sig):
            return False

        payload = json.loads(payload_str)
        created_at = payload.get("t", 0)
        if time.time() - created_at > SESSION_MAX_AGE:
            return False

        return True
    except Exception:
        return False


# ── 不需要认证的路径 ──

PUBLIC_PATHS = {
    "/login",
    "/api/auth/login",
    "/api/auth/check",
    "/favicon.ico",
}


def is_public_path(path: str) -> bool:
    """判断路径是否无需认证"""
    return path in PUBLIC_PATHS
