"""服务管理器 - 管理所有服务的生命周期"""
import logging
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class ServiceManager:
    """服务管理器"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.engines = {}
        self.running = False
    
    def initialize(self):
        """初始化所有服务"""
        logger.info("初始化服务...")
        
        # 初始化事件存储
        from app.core.events import EventStore
        from config_us import DATABASE_CONFIG
        self.event_store = EventStore(DATABASE_CONFIG["path"])
        logger.info("✅ 事件存储就绪")
        
        # 初始化风险引擎
        self._init_risk_engine()
        
        # 初始化执行引擎
        self._init_execution_engine()
        
        # 初始化决策引擎 (由调度器接管)
        logger.info("✅ 决策引擎：由 TradingScheduler 接管")
        
        logger.info("✅ 所有服务初始化完成")
    
    def _init_risk_engine(self):
        """初始化风险引擎"""
        from app.risk.engine import RiskEngine
        from config_us import RISK_CONFIG
        
        self.engines['risk'] = RiskEngine(self.config, self.event_store)
        self.engines['risk'].initialize()
        logger.info("✅ 风险引擎就绪")
    
    def _init_execution_engine(self):
        """初始化执行引擎"""
        import os
        from app.execution.engine import ExecutionEngine
        from config_us import EXECUTION_CONFIG, FUTU_OPEND_HOST, FUTU_OPEND_PORT, TRADE_ENV, SIMULATE_ACCOUNT_ID

        # 实盘用 REAL_ACCOUNT_ID，模拟盘用 SIMULATE_ACCOUNT_ID
        _account_id = os.getenv("REAL_ACCOUNT_ID", "") if TRADE_ENV == 0 else SIMULATE_ACCOUNT_ID

        exec_config = {
            **EXECUTION_CONFIG,
            "futu_host": FUTU_OPEND_HOST,
            "futu_port": FUTU_OPEND_PORT,
            "futu_trade_env": TRADE_ENV,
            "account_id": _account_id,
        }
        
        self.engines['execution'] = ExecutionEngine(exec_config, self.event_store)
        self.engines['execution'].initialize()
        logger.info("✅ 执行引擎就绪")
    
    def start(self):
        """启动所有服务"""
        logger.info("启动服务...")
        self.running = True
        
        for name, engine in self.engines.items():
            if hasattr(engine, 'start'):
                engine.start()
        
        logger.info("✅ 所有服务已启动")
    
    def stop(self):
        """停止所有服务"""
        logger.info("停止服务...")
        self.running = False
        
        for name, engine in self.engines.items():
            if hasattr(engine, 'shutdown'):
                engine.shutdown()
        
        logger.info("✅ 所有服务已停止")
    
    def get_engine(self, name: str):
        """获取引擎"""
        return self.engines.get(name)
    
    def is_healthy(self) -> bool:
        """检查服务健康状态"""
        for name, engine in self.engines.items():
            if hasattr(engine, 'is_healthy'):
                if not engine.is_healthy():
                    logger.warning(f"服务不健康：{name}")
                    return False
        return True
