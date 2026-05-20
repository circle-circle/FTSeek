#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 异常层次 — 业务异常分类

架构位置:
  app/core/exceptions.py (基础异常类)
    ↓ 被各模块导入

设计目的:
  1. 替代通用的 `except Exception`，提供语义化异常类型
  2. 支持按异常类型分层处理 (网络错误/数据错误/风控错误等)
  3. 便于日志分类和监控告警

约束:
  - 所有异常继承自 FTSeekError
  - 异常名称必须语义清晰
  - 支持错误代码 (error_code) 用于监控统计

版本历史:
  2026-05-19 v1: 创建异常层次 (Phase 9B)
"""


class FTSeekError(Exception):
    """FTSeek 基础异常类"""

    def __init__(self, message: str, error_code: int = 0):
        self.message = message
        self.error_code = error_code
        super().__init__(message)


# ============================================================
#  网络错误层 (NetworkError)
# ============================================================

class NetworkError(FTSeekError):
    """网络相关错误"""
    pass


class FutuAPIError(NetworkError):
    """Futu API 调用失败"""

    def __init__(self, message: str, error_code: int = 0):
        super().__init__(f"[FutuAPI] {message}", error_code)


class DataFetchError(NetworkError):
    """数据获取失败 (行情/K 线/资讯等)"""

    def __init__(self, message: str, error_code: int = 0):
        super().__init__(f"[DataFetch] {message}", error_code)


# ============================================================
#  数据错误层 (DataError)
# ============================================================

class DataError(FTSeekError):
    """数据质量问题"""
    pass


class StaleDataError(DataError):
    """数据过期 (超过缓存有效期)"""

    def __init__(self, message: str, error_code: int = 0):
        super().__init__(f"[StaleData] {message}", error_code)


class InvalidDataError(DataError):
    """数据无效 (格式错误/缺失字段等)"""

    def __init__(self, message: str, error_code: int = 0):
        super().__init__(f"[InvalidData] {message}", error_code)


# ============================================================
#  风控错误层 (RiskError)
# ============================================================

class RiskError(FTSeekError):
    """风控相关错误"""
    pass


class CircuitBreakerError(RiskError):
    """熔断器触发"""

    def __init__(self, message: str, error_code: int = 0):
        super().__init__(f"[CircuitBreaker] {message}", error_code)


class RiskCheckError(RiskError):
    """RiskCheck 执行失败"""

    def __init__(self, message: str, error_code: int = 0):
        super().__init__(f"[RiskCheck] {message}", error_code)


# ============================================================
#  引擎错误层 (EngineError)
# ============================================================

class EngineError(FTSeekError):
    """引擎/服务错误"""
    pass


class ProviderNotInjectedError(EngineError):
    """Provider 未注入 (DI 配置缺失)"""

    def __init__(self, provider_name: str):
        super().__init__(
            f"Provider not injected: {provider_name}. "
            f"Check DI Container configuration.",
            error_code=5001
        )


class ServiceError(EngineError):
    """服务层错误 (ExitExecutor/ConsensusExecutor 等)"""

    def __init__(self, message: str, error_code: int = 0):
        super().__init__(f"[Service] {message}", error_code)


# ============================================================
#  配置错误层 (ConfigError)
# ============================================================

class ConfigError(FTSeekError):
    """配置相关错误"""
    pass


class ConfigValidationError(ConfigError):
    """配置验证失败"""

    def __init__(self, message: str, error_code: int = 0):
        super().__init__(f"[ConfigValidation] {message}", error_code)


class ConfigLoadError(ConfigError):
    """配置文件加载失败"""

    def __init__(self, message: str, error_code: int = 0):
        super().__init__(f"[ConfigLoad] {message}", error_code)


# ============================================================
#  工具函数
# ============================================================

def is_transient_error(error: Exception) -> bool:
    """
    判断是否为瞬态错误 (可重试)

    Args:
        error: 异常对象

    Returns:
        True 如果错误是瞬态的 (网络超时/数据延迟等), False 如果应终止
    """
    transient_types = (NetworkError, StaleDataError)
    return isinstance(error, transient_types)


def get_error_category(error: Exception) -> str:
    """
    获取错误分类 (用于监控统计)

    Args:
        error: 异常对象

    Returns:
        错误分类字符串: 'network', 'data', 'risk', 'engine', 'config', 'unknown'
    """
    if isinstance(error, NetworkError):
        return 'network'
    elif isinstance(error, DataError):
        return 'data'
    elif isinstance(error, RiskError):
        return 'risk'
    elif isinstance(error, EngineError):
        return 'engine'
    elif isinstance(error, ConfigError):
        return 'config'
    else:
        return 'unknown'


__all__ = [
    "FTSeekError",
    "NetworkError",
    "FutuAPIError",
    "DataFetchError",
    "DataError",
    "StaleDataError",
    "InvalidDataError",
    "RiskError",
    "CircuitBreakerError",
    "RiskCheckError",
    "EngineError",
    "ProviderNotInjectedError",
    "ServiceError",
    "ConfigError",
    "ConfigValidationError",
    "ConfigLoadError",
    "is_transient_error",
    "get_error_category",
]
