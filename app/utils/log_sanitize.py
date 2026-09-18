"""日志脱敏工具 (SSOT for log secret scrubbing)

背景: `requests` 的异常文本自带完整 URL
(例: "Max retries exceeded with url: /query?function=OVERVIEW&apikey=XXXX")。
只要 API Key 走 query string (AlphaVantage 系全部只支持这种写法),
直接把异常对象塞进日志就会把密钥写进日志文件。

用法: 记录请求异常时包一层 —— `logger.error(f"...: {scrub_secrets(e)}")`。
TwelveData 侧已改为 Authorization header 携钥 (URL 不再含 key), 无需此处兜底。
"""
import re
from typing import Any

# 密钥类 query 参数名 (大小写不敏感)
_SECRET_PARAM_NAMES = (
    "apikey",
    "api_key",
    "apikeyid",
    "token",
    "access_token",
    "auth",
    "password",
    "secret",
)
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_PARAM_NAMES) + r")=([^&\s'\"]+)"
)


def scrub_secrets(text: Any) -> str:
    """把文本中密钥类参数的值替换为 *** (保留参数名与其余上下文)。"""
    return _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}=***", str(text))