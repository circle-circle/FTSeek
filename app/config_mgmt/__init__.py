"""
FTSeek-US 配置治理模块

三层防御机制:
1. ConfigValidator     — 启动时强制校验必需配置键
2. ConfigDriftDetector — 定期巡检配置漂移 (默认 4 小时)
3. ConfigHotReloader   — 文件监控 + 热重载 (60s 冷却)

用法:
    from app.config_mgmt import ConfigValidator, ConfigDriftDetector, ConfigHotReloader
    
    # 启动时校验
    validator = ConfigValidator(config_loader)
    errors = validator.validate_all()
    if errors:
        raise ValueError(f"配置校验失败: {errors}")
    
    # 漂移检测 (后台线程)
    detector = ConfigDriftDetector(config_loader, interval_hours=4)
    detector.start()
    
    # 热重载 (后台线程)
    reloader = ConfigHotReloader(config_loader, cooldown_seconds=60)
    reloader.start()
"""

from app.config_mgmt.validator import ConfigValidator
from app.config_mgmt.drift_detector import ConfigDriftDetector
from app.config_mgmt.hot_reloader import ConfigHotReloader

__all__ = ["ConfigValidator", "ConfigDriftDetector", "ConfigHotReloader"]
