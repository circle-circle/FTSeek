"""应用引导类 - 负责应用初始化和启动"""
import logging
from typing import Dict, Any
from pathlib import Path

logger = logging.getLogger(__name__)

class AppBootstrap:
    """应用引导类"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.services = {}
    
    def initialize(self):
        """初始化应用"""
        logger.info("初始化 FTSeek-US 应用...")
        
        # 加载服务
        self._load_services()
        
        # 初始化引擎
        self._init_engines()
        
        logger.info("✅ FTSeek-US 应用初始化完成")
    
    def _load_services(self):
        """加载服务"""
        from app.services.service_manager import ServiceManager
        self.services['service_manager'] = ServiceManager(self.config)
        self.services['service_manager'].initialize()
    
    def _init_engines(self):
        """初始化引擎"""
        # 由 ServiceManager 管理引擎初始化
        pass
    
    def start(self):
        """启动应用"""
        logger.info("启动 FTSeek-US 应用...")
        for name, service in self.services.items():
            logger.info(f"启动服务：{name}")
            service.start()
        logger.info("✅ FTSeek-US 应用已启动")
    
    def stop(self):
        """停止应用"""
        logger.info("停止 FTSeek-US 应用...")
        for name, service in reversed(list(self.services.items())):
            logger.info(f"停止服务：{name}")
            service.stop()
        logger.info("✅ FTSeek-US 应用已停止")
