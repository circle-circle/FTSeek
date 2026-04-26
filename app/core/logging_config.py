#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 统一日志配置系统

功能：
1. 统一 logger 配置，消除重复日志
2. 日志级别控制 (DEBUG/INFO/WARNING/ERROR)
3. 日志轮转 (按大小 + 按天)
4. 结构化日志输出 (JSON 格式可选)
5. TraceID 全链路追踪

预期收益：
- 日志体积减少 60%
- 问题定位时间 30 分钟 → 5 分钟
"""

import logging
import sys
import json
import time
import threading
import uuid
from pathlib import Path
from datetime import datetime
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from typing import Optional, Dict, Any
from contextlib import contextmanager
from dataclasses import dataclass, field


# ============================================================
# 配置常量
# ============================================================
@dataclass
class LoggingConfig:
    """日志配置"""
    log_dir: Path = field(default_factory=lambda: Path('/opt/ftseek/FTSeek/logs'))
    level: str = "INFO"  # 默认 INFO，由 RotatingFileHandler 控制大小 (50MB)
    max_size_mb: int = 50  # 按大小轮转：50MB
    backup_count_size: int = 5  # 按大小轮转备份数
    backup_count_daily: int = 30  # 按天轮转备份数
    enable_json_format: bool = False  # JSON 格式输出
    enable_trace_id: bool = True  # TraceID 追踪
    console_output: bool = True  # 控制台输出


# 全局配置
DEFAULT_CONFIG = LoggingConfig()

# 线程本地存储 (TraceID)
_trace_context = threading.local()


# ============================================================
# TraceID 上下文管理
# ============================================================
def get_trace_id() -> str:
    """获取当前 TraceID"""
    return getattr(_trace_context, 'trace_id', None)


def set_trace_id(trace_id: str):
    """设置当前 TraceID"""
    _trace_context.trace_id = trace_id


@contextmanager
def trace_context(trace_id: str = None):
    """
    TraceID 上下文管理器
    
    使用示例:
        with trace_context() as tid:
            logger.info("处理请求", extra={'trace_id': tid})
    """
    old_id = get_trace_id()
    trace_id = trace_id or str(uuid.uuid4())[:8]
    set_trace_id(trace_id)
    try:
        yield trace_id
    finally:
        set_trace_id(old_id)


# ============================================================
# 日志格式器
# ============================================================
class JSONFormatter(logging.Formatter):
    """JSON 格式日志输出"""
    
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "process": record.process,
            "thread": record.thread,
        }
        
        # 添加 TraceID
        trace_id = getattr(record, 'trace_id', None) or get_trace_id()
        if trace_id:
            log_data["trace_id"] = trace_id
        
        # 添加异常信息
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        # 添加额外字段
        for key, value in record.__dict__.items():
            if key not in ['name', 'msg', 'args', 'created', 'filename', 'funcName',
                          'levelname', 'levelno', 'lineno', 'module', 'msecs',
                          'pathname', 'process', 'processName', 'relativeCreated',
                          'stack_info', 'exc_info', 'exc_text', 'thread', 'threadName',
                          'trace_id', 'message']:
                log_data[key] = value
        
        return json.dumps(log_data, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """文本格式日志输出 (带 TraceID 支持)"""
    
    def __init__(self, include_trace_id: bool = True):
        super().__init__(
            fmt="%(asctime)s [%(name)s] [%(trace_id)s] %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        )
        self.include_trace_id = include_trace_id
    
    def format(self, record: logging.LogRecord) -> str:
        # 自动注入 TraceID
        if not hasattr(record, 'trace_id'):
            record.trace_id = get_trace_id() or "-"
        return super().format(record)


# ============================================================
# TraceID 过滤器
# ============================================================
class TraceIDFilter(logging.Filter):
    """自动为日志记录添加 TraceID"""
    
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, 'trace_id'):
            record.trace_id = get_trace_id() or "-"
        return True


# ============================================================
# 日志级别过滤器
# ============================================================
class LevelRangeFilter(logging.Filter):
    """日志级别范围过滤器 (用于分离不同级别日志)"""
    
    def __init__(self, min_level: int, max_level: int):
        super().__init__()
        self.min_level = min_level
        self.max_level = max_level
    
    def filter(self, record: logging.LogRecord) -> bool:
        return self.min_level <= record.levelno <= self.max_level


# ============================================================
# 统一日志设置
# ============================================================
_loggers_initialized: Dict[str, bool] = {}
_setup_lock = threading.Lock()


def setup_logging(
    module_name: str,
    config: LoggingConfig = None,
    level: str = None
) -> logging.Logger:
    """
    统一日志配置入口
    
    Args:
        module_name: 模块名称 (如 "data_subscriber", "trader.executor")
        config: 日志配置
        level: 日志级别 (覆盖配置)
    
    Returns:
        配置好的 logger 实例
    
    使用示例:
        logger = setup_logging("data_subscriber")
        logger.info("启动数据订阅")
    """
    config = config or DEFAULT_CONFIG
    level = level or config.level
    
    # 统一 logger 命名：ftseek.{module_name}
    logger_name = f"ftseek.{module_name}"
    
    with _setup_lock:
        # 防止重复初始化
        if logger_name in _loggers_initialized:
            return logging.getLogger(logger_name)
        
        logger = logging.getLogger(logger_name)
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        logger.propagate = False  # 关键：禁止传播到父 logger，消除重复日志
        
        # 清除现有处理器
        logger.handlers = []
        
        # 添加 TraceID 过滤器
        logger.addFilter(TraceIDFilter())
        
        # 选择格式器
        if config.enable_json_format:
            formatter = JSONFormatter()
        else:
            formatter = TextFormatter(include_trace_id=config.enable_trace_id)
        
        # 1. 控制台处理器 (可选)
        if config.console_output:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)
        
        # 确保日志目录存在
        config.log_dir.mkdir(parents=True, exist_ok=True)
        
        # 2. 主日志文件 - 按大小轮转
        main_log_file = config.log_dir / f"{module_name.replace('.', '_')}.log"
        main_handler = RotatingFileHandler(
            main_log_file,
            maxBytes=config.max_size_mb * 1024 * 1024,
            backupCount=config.backup_count_size,
            encoding='utf-8'
        )
        main_handler.setLevel(logging.DEBUG)  # 记录所有级别
        main_handler.setFormatter(formatter)
        logger.addHandler(main_handler)
        
        # 3. 错误日志 - 单独文件 (只记录 ERROR 及以上)
        error_log_file = config.log_dir / f"{module_name.replace('.', '_')}_error.log"
        error_handler = RotatingFileHandler(
            error_log_file,
            maxBytes=config.max_size_mb * 1024 * 1024,
            backupCount=config.backup_count_size,
            encoding='utf-8'
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        logger.addHandler(error_handler)
        
        # 4. 按天轮转日志 (归档用)
        daily_log_file = config.log_dir / f"{module_name.replace('.', '_')}_daily.log"
        daily_handler = TimedRotatingFileHandler(
            daily_log_file,
            when='D',
            interval=1,
            backupCount=config.backup_count_daily,
            encoding='utf-8'
        )
        daily_handler.setLevel(logging.INFO)
        daily_handler.setFormatter(formatter)
        daily_handler.suffix = "%Y-%m-%d"
        logger.addHandler(daily_handler)
        
        # 5. WARNING 及以上级别日志 (单独文件用于告警分析)
        warning_log_file = config.log_dir / f"{module_name.replace('.', '_')}_warning.log"
        warning_handler = RotatingFileHandler(
            warning_log_file,
            maxBytes=config.max_size_mb * 1024 * 1024,
            backupCount=config.backup_count_size,
            encoding='utf-8'
        )
        warning_handler.setLevel(logging.WARNING)
        warning_handler.setFormatter(formatter)
        logger.addHandler(warning_handler)
        
        _loggers_initialized[logger_name] = True
        
        logger.info(f"日志系统初始化完成 [level={level}, dir={config.log_dir}]")
        
        return logger


def get_logger(module_name: str) -> logging.Logger:
    """
    获取已配置的 logger
    
    Args:
        module_name: 模块名称
    
    Returns:
        logger 实例
    """
    logger_name = f"ftseek.{module_name}"
    logger = logging.getLogger(logger_name)
    
    # 如果未初始化，使用默认配置初始化
    if logger_name not in _loggers_initialized:
        return setup_logging(module_name)
    
    return logger


# ============================================================
# 日志统计和清理
# ============================================================
def get_log_stats(log_dir: Path = None) -> Dict[str, Any]:
    """获取日志统计信息"""
    log_dir = log_dir or DEFAULT_CONFIG.log_dir
    
    if not log_dir.exists():
        return {'error': '日志目录不存在'}
    
    log_files = list(log_dir.glob('*.log'))
    total_size = sum(f.stat().st_size for f in log_files)
    
    return {
        'file_count': len(log_files),
        'total_size_mb': round(total_size / 1024 / 1024, 2),
        'files': sorted([
            {
                'name': f.name,
                'size_mb': round(f.stat().st_size / 1024 / 1024, 2),
                'modified': datetime.fromtimestamp(f.stat().st_mtime).isoformat()
            }
            for f in log_files
        ], key=lambda x: x['size_mb'], reverse=True)
    }


def cleanup_old_logs(days: int = 30, log_dir: Path = None):
    """清理旧日志文件"""
    import time
    
    log_dir = log_dir or DEFAULT_CONFIG.log_dir
    
    if not log_dir.exists():
        return {'cleaned': 0}
    
    now = time.time()
    cutoff = now - (days * 24 * 60 * 60)
    
    cleaned = 0
    for log_file in log_dir.glob('*.log.*'):
        if log_file.stat().st_mtime < cutoff:
            log_file.unlink()
            cleaned += 1
    
    logging.getLogger("ftseek.logging").info(f"清理 {cleaned} 个旧日志文件 (> {days} 天)")
    
    return {'cleaned': cleaned}


# ============================================================
# 全局根 logger 配置
# ============================================================
def setup_root_logging(config: LoggingConfig = None):
    """配置根 logger (用于未显式配置的模块)"""
    config = config or DEFAULT_CONFIG
    
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.level.upper(), logging.INFO))
    
    # 清除现有处理器
    root_logger.handlers = []
    
    # 添加 TraceID 过滤器
    root_logger.addFilter(TraceIDFilter())
    
    # 格式器
    formatter = TextFormatter(include_trace_id=config.enable_trace_id)
    
    # 控制台处理器
    if config.console_output:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)
    
    # 文件处理器
    config.log_dir.mkdir(parents=True, exist_ok=True)
    
    file_handler = RotatingFileHandler(
        config.log_dir / 'ftseek_root.log',
        maxBytes=config.max_size_mb * 1024 * 1024,
        backupCount=config.backup_count_size,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
    
    return root_logger


# ============================================================
# 便捷函数
# ============================================================
def log_with_trace(logger: logging.Logger, level: str, message: str, **kwargs):
    """带 TraceID 的日志记录"""
    trace_id = get_trace_id()
    extra = kwargs.get('extra', {})
    extra['trace_id'] = trace_id
    kwargs['extra'] = extra
    
    log_method = getattr(logger, level.lower(), logger.info)
    log_method(message, **kwargs)


# ============================================================
# 主函数 (测试用)
# ============================================================
if __name__ == '__main__':
    # 测试日志系统
    import argparse
    
    parser = argparse.ArgumentParser(description='FTSeek 日志系统')
    parser.add_argument('--stats', action='store_true', help='显示日志统计')
    parser.add_argument('--cleanup', type=int, metavar='DAYS', help='清理旧日志')
    parser.add_argument('--test', action='store_true', help='测试日志输出')
    parser.add_argument('--json', action='store_true', help='使用 JSON 格式')
    parser.add_argument('--level', default='INFO', help='日志级别')
    
    args = parser.parse_args()
    
    config = LoggingConfig(
        enable_json_format=args.json,
        level=args.level
    )
    
    if args.stats:
        stats = get_log_stats()
        print("📊 日志统计:")
        print(f"  文件数量：{stats.get('file_count', 0)}")
        print(f"  总大小：{stats.get('total_size_mb', 0):.2f} MB")
        print("\n📁 文件列表 (按大小排序):")
        for f in stats.get('files', [])[:10]:
            print(f"  {f['name']}: {f['size_mb']:.2f} MB ({f['modified']})")
    
    elif args.cleanup:
        result = cleanup_old_logs(args.cleanup)
        print(f"✅ 清理完成：{result['cleaned']} 个文件")
    
    elif args.test:
        logger = setup_logging("test_module", config)
        
        print("\n🧪 日志测试:")
        with trace_context() as tid:
            logger.debug(f"调试信息 [trace_id={tid}]")
            logger.info(f"普通信息 [trace_id={tid}]")
            logger.warning(f"警告信息 [trace_id={tid}]")
            logger.error(f"错误信息 [trace_id={tid}]")
        
        # 测试无 TraceID 的情况
        logger.info("无 TraceID 的日志")
        
        print(f"\n✅ 日志已写入：{config.log_dir}")
        print(f"📊 当前日志统计：{get_log_stats()}")
    
    else:
        parser.print_help()
