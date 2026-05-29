"""FTSeek-US 统一异常定义

所有业务异常继承自 FTSeekException，便于：
1. 上层统一捕获和分类处理
2. 标准化错误日志格式
3. 监控告警系统识别异常类型
"""
import logging
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class FTSeekException(Exception):
    """所有 FTSeek 业务异常的基类"""

    def __init__(self, code: str, message: str, context: Optional[Dict] = None):
        self.code = code
        self.message = message
        self.context = context or {}
        super().__init__(f"[{code}] {message}")

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "context": self.context,
        }


class ConfigurationError(FTSeekException):
    """配置错误 - 配置缺失、冲突或格式错误"""

    def __init__(self, message: str, context: Optional[Dict] = None):
        super().__init__("CONFIG_ERROR", message, context)


class DataInconsistencyError(FTSeekException):
    """数据不一致 - 内存/DB状态不同步"""

    def __init__(self, message: str, context: Optional[Dict] = None):
        super().__init__("DATA_INCONSISTENCY", message, context)


class PositionSyncError(FTSeekException):
    """持仓同步失败"""

    def __init__(self, message: str, context: Optional[Dict] = None):
        super().__init__("POSITION_SYNC_ERROR", message, context)


class RiskLimitError(FTSeekException):
    """风控限制触发"""

    def __init__(self, message: str, context: Optional[Dict] = None):
        super().__init__("RISK_LIMIT", message, context)


class ExternalServiceError(FTSeekException):
    """外部服务（LLM/行情/推送）调用失败"""

    def __init__(self, service: str, message: str, context: Optional[Dict] = None):
        ctx = context or {}
        ctx["service"] = service
        super().__init__("EXTERNAL_SERVICE_ERROR", f"{service}: {message}", ctx)
