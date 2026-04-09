#!/usr/bin/env python3
"""
FTSeek 修复版主入口
修复异步事件循环问题
"""
import sys
import os
import asyncio
import threading
import signal
import logging
import time

# 设置环境变量（必须在导入 futu 之前）
os.environ["FUTU_LOG_LEVEL"] = "ERROR"
os.environ["FUTU_LOG_TO_FILE"] = "0"
os.environ["FUTU_LOG_PATH"] = "/tmp/futu_logs"

# 确保 app 包可被 import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 创建临时日志目录
try:
    os.makedirs("/tmp/futu_logs", exist_ok=True)
except (OSError, PermissionError):
    pass

# 配置 futu 日志
import logging as stdlib_logging
futu_logger = stdlib_logging.getLogger("futu")
futu_logger.setLevel(stdlib_logging.CRITICAL)
futu_logger.handlers = []
futu_logger.propagate = False

def ensure_event_loop():
    """确保当前线程有事件循环"""
    try:
        loop = asyncio.get_event_loop()
        print(f"✅ 当前线程已有事件循环: {loop}")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        print(f"✅ 已创建新事件循环: {loop}")
    return loop

def main():
    """主函数"""
    print("🚀 FTSeek 修复版启动...")
    
    # 确保事件循环
    loop = ensure_event_loop()
    
    # 导入配置
    from app import config
    
    # 配置日志
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_DIR / "ftseek.log", encoding="utf-8")
        ]
    )
    
    logger = logging.getLogger("ftseek.main")
    
    try:
        # 导入其他模块
        from app.strategy import STRATEGY_REGISTRY
        from app.strategy.strategy_manager import strategy_manager
        from app.notifier.dingtalk import DingTalkNotifier
        from app.trader.executor import TradeExecutor
        from app.data_subscriber import DataSubscriber
        from app.server import app as fastapi_app, set_executor, set_notifier, set_strategy_manager
        from app.risk import get_risk_manager, init_realtime_monitor
        
        logger.info("模块导入成功")
        
        # 初始化组件
        risk_manager = get_risk_manager()
        notifier = DingTalkNotifier()
        executor = TradeExecutor()
        
        # 注册策略
        for strategy_cls in STRATEGY_REGISTRY.values():
            strategy_manager.register(strategy_cls())
        
        logger.info(f"已注册 {len(STRATEGY_REGISTRY)} 个策略")
        
        # 设置全局单例
        set_executor(executor)
        set_notifier(notifier)
        set_strategy_manager(strategy_manager)
        
        # 初始化实时监控
        init_realtime_monitor()
        
        # 创建数据订阅器
        strategies = list(strategy_manager.get_strategies().values())
        subscriber = DataSubscriber(strategies, notifier)
        
        # 启动数据订阅
        logger.info("启动数据订阅...")
        subscriber.start()
        
        # 启动 FastAPI 服务器
        import uvicorn
        
        def run_fastapi():
            uvicorn.run(
                fastapi_app,
                host=config.API_HOST,
                port=config.API_PORT,
                log_level="info"
            )
        
        # 在子线程中运行 FastAPI
        api_thread = threading.Thread(target=run_fastapi, daemon=True)
        api_thread.start()
        
        logger.info(f"API 服务器启动: http://{config.API_HOST}:{config.API_PORT}")
        
        # 主循环
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("收到中断信号，正在关闭...")
            subscriber.stop()
            logger.info("FTSeek 已关闭")
            
    except Exception as e:
        logger.error(f"启动失败: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
