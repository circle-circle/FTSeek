"""
日志轮转配置

防止日志文件无限增长
"""
import logging
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from pathlib import Path
import os

# 日志目录
LOGS_DIR = Path('/opt/ftseek/FTSeek/logs')
LOGS_DIR.mkdir(parents=True, exist_ok=True)


def setup_logging(level=logging.INFO):
    """
    配置日志系统
    
    Args:
        level: 日志级别
    """
    # 根日志器
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    
    # 清除现有处理器
    root_logger.handlers = []
    
    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_formatter = logging.Formatter(
        '%(asctime)s | %(process)d | %(thread)d | [%(filename)s:%(lineno)d] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)
    
    # 文件处理器 - 按大小轮转 (10MB)
    file_handler = RotatingFileHandler(
        LOGS_DIR / 'ftseek.log',
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=10,
        encoding='utf-8'
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(console_formatter)
    root_logger.addHandler(file_handler)
    
    # 错误日志 - 单独文件
    error_handler = RotatingFileHandler(
        LOGS_DIR / 'error.log',
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(console_formatter)
    root_logger.addHandler(error_handler)
    
    # 按日期轮转的日志 (每天)
    daily_handler = TimedRotatingFileHandler(
        LOGS_DIR / 'ftseek_daily.log',
        when='D',
        interval=1,
        backupCount=30,
        encoding='utf-8'
    )
    daily_handler.setLevel(logging.INFO)
    daily_handler.setFormatter(console_formatter)
    root_logger.addHandler(daily_handler)
    
    # 交易日志 - 单独文件
    trade_logger = logging.getLogger('ftseek.execution')
    trade_handler = RotatingFileHandler(
        LOGS_DIR / 'trades.log',
        maxBytes=10 * 1024 * 1024,
        backupCount=30,
        encoding='utf-8'
    )
    trade_handler.setLevel(logging.INFO)
    trade_formatter = logging.Formatter(
        '%(asctime)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    trade_handler.setFormatter(trade_formatter)
    trade_logger.addHandler(trade_handler)
    
    # 风险预警日志 - 单独文件
    risk_logger = logging.getLogger('ftseek.risk')
    risk_handler = RotatingFileHandler(
        LOGS_DIR / 'risk.log',
        maxBytes=10 * 1024 * 1024,
        backupCount=30,
        encoding='utf-8'
    )
    risk_handler.setLevel(logging.WARNING)
    risk_handler.setFormatter(console_formatter)
    risk_logger.addHandler(risk_handler)
    
    logging.info("日志系统已配置")
    logging.info(f"日志目录：{LOGS_DIR}")
    logging.info("日志轮转：10MB/文件，保留 10 个备份")


def get_log_stats() -> dict:
    """获取日志统计"""
    if not LOGS_DIR.exists():
        return {'error': '日志目录不存在'}
    
    log_files = list(LOGS_DIR.glob('*.log'))
    
    total_size = sum(f.stat().st_size for f in log_files)
    
    return {
        'file_count': len(log_files),
        'total_size_mb': total_size / 1024 / 1024,
        'files': [
            {
                'name': f.name,
                'size_mb': f.stat().st_size / 1024 / 1024,
                'modified': f.stat().st_mtime
            }
            for f in log_files
        ]
    }


def cleanup_old_logs(days: int = 30):
    """清理旧日志"""
    import time
    
    if not LOGS_DIR.exists():
        return
    
    now = time.time()
    cutoff = now - (days * 24 * 60 * 60)
    
    cleaned = 0
    
    for log_file in LOGS_DIR.glob('*.log.*'):
        if log_file.stat().st_mtime < cutoff:
            log_file.unlink()
            cleaned += 1
    
    logging.info(f"清理 {cleaned} 个旧日志文件 (> {days} 天)")


if __name__ == '__main__':
    import sys
    
    setup_logging()
    
    if len(sys.argv) > 1:
        if sys.argv[1] == '--stats':
            stats = get_log_stats()
            print("日志统计:")
            print(f"  文件数量：{stats.get('file_count', 0)}")
            print(f"  总大小：{stats.get('total_size_mb', 0):.2f} MB")
            print("\n文件列表:")
            for f in stats.get('files', []):
                print(f"  {f['name']}: {f['size_mb']:.2f} MB")
        
        elif sys.argv[1] == '--cleanup':
            days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
            cleanup_old_logs(days)
    
    else:
        # 测试日志
        logging.info("日志系统测试")
        logging.debug("调试信息")
        logging.warning("警告信息")
        logging.error("错误信息")
        
        print(f"日志已写入：{LOGS_DIR}")
