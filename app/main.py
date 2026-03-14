"""
FTSeek 主入口
启动流程: OpenD -> 数据订阅 -> 策略引擎 -> FastAPI回调服务
"""
import sys
import os
import signal
import logging
import threading
import time
import subprocess

# 确保 app 包可被 import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ============================================================
#  富途日志配置 (必须在导入 futu 模块之前设置)
# ============================================================
os.environ["FUTU_LOG_LEVEL"] = "ERROR"
os.environ["FUTU_LOG_TO_FILE"] = "0"
os.environ["FUTU_LOG_PATH"] = "/tmp/futu_logs"

# 创建临时日志目录
import logging as stdlib_logging
try:
    os.makedirs("/tmp/futu_logs", exist_ok=True)
    # 提前配置 futu 日志，防止模块导入时初始化文件日志
    futu_logger = stdlib_logging.getLogger("futu")
    futu_logger.setLevel(stdlib_logging.CRITICAL)
    futu_logger.handlers = []
except:
    pass

from app import config
from app.strategy import STRATEGY_REGISTRY
from app.strategy.strategy_manager import strategy_manager
from app.notifier.dingtalk import DingTalkNotifier
from app.trader.executor import TradeExecutor
from app.data_subscriber import DataSubscriber
from app.server import app as fastapi_app, set_executor, set_notifier, set_strategy_manager
from app.risk import get_risk_manager, init_realtime_monitor

# 初始化 risk_manager 单例
risk_manager = get_risk_manager()

# ============================================================
#  日志配置
# ============================================================
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(config.LOG_DIR / "ftseek.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("ftseek.main")


# ============================================================
#  OpenD 管理
# ============================================================
class OpenDManager:
    """管理 OpenD 进程"""

    def __init__(self):
        self.process = None
        self.opend_path = os.path.join(config.OPEND_DIR, "FutuOpenD")

    def is_running(self) -> bool:
        try:
            result = subprocess.run(
                ["pgrep", "-f", "FutuOpenD"],
                capture_output=True, text=True
            )
            return result.returncode == 0
        except:
            return False

    def start(self):
        if self.is_running():
            logger.info("OpenD 已在运行")
            return True

        if not os.path.isfile(self.opend_path):
            logger.error(f"OpenD 二进制不存在: {self.opend_path}")
            return False

        logger.info("启动 OpenD...")
        self.process = subprocess.Popen(
            [self.opend_path],
            cwd=config.OPEND_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        time.sleep(5)

        if self.process.poll() is not None:
            logger.error("OpenD 启动失败")
            return False

        logger.info(f"OpenD 已启动, PID={self.process.pid}")
        return True

    def stop(self):
        if self.process:
            self.process.terminate()
            self.process.wait(timeout=10)
            logger.info("OpenD 已停止")


# ============================================================
#  主逻辑
# ============================================================
def main():
    logger.info("=" * 60)
    logger.info("FTSeek 启动中...")
    logger.info(f"交易环境: {'模拟' if config.TRADE_ENV == 1 else '真实'}")
    logger.info(f"监控标的: {config.WATCH_LIST}")
    logger.info("=" * 60)

    # 1) 启动 OpenD
    opend = OpenDManager()
    if not opend.start():
        logger.warning("OpenD 未能启动，将以离线模式运行 (仅 API 服务)")

    # 2) 初始化组件
    notifier = DingTalkNotifier()
    executor = TradeExecutor()

    # 注入到 FastAPI
    set_executor(executor)
    set_notifier(notifier)

    # 初始化风险监控
    from app.risk.realtime_monitor import init_realtime_monitor
    from app.risk.dingtalk_alert import init_dingtalk_alert
    
    # 初始化钉钉警报
    dingtalk_sender = init_dingtalk_alert()
    logger.info("钉钉警报通知已初始化")
    
    # 启动风险监控（带钉钉通知回调）
    monitor = init_realtime_monitor(risk_manager, alert_callbacks=[dingtalk_sender.send_alert])
    monitor.start()
    logger.info("实时风险监控已启动")

    # 3) 初始化全部策略 (通过注册表自动发现)
    for name, StrategyClass in STRATEGY_REGISTRY.items():
        cfg = config.STRATEGIES.get(name, {})
        s = StrategyClass(cfg)
        strategy_manager.register(s)

    # 注入策略管理器到 API 服务
    set_strategy_manager(strategy_manager)

    # 获取已启用的策略列表供数据订阅使用
    strategies = strategy_manager.enabled()
    enabled_names = [s.name for s in strategies]
    all_names = [s.name for s in strategy_manager.all()]
    logger.info(f"策略已加载: {all_names}")
    logger.info(f"策略已启用: {enabled_names}")

    # 4) 启动数据订阅 (如果 OpenD 在运行)
    subscriber = None
    if opend.is_running():
        try:
            subscriber = DataSubscriber(strategies, notifier)
            subscriber.start()
        except Exception as e:
            logger.error(f"数据订阅启动失败: {e}")
            logger.info("将以纯 API 模式运行")

    # 5) 发送启动通知
    strategy_summary = "\n".join(
        f"  {'✅' if s.enabled else '❌'} {s.name}"
        for s in strategy_manager.all()
    )
    notifier.send_text(
        f"[FTSeek] 系统已启动\n"
        f"环境: {'模拟' if config.TRADE_ENV == 1 else '真实'}\n"
        f"标的: {', '.join(config.WATCH_LIST)}\n"
        f"策略:\n{strategy_summary}\n"
        f"回调: {config.CALLBACK_EXTERNAL_URL}\n"
        f"TradingView Webhook: {config.CALLBACK_EXTERNAL_URL}/api/tradingview/webhook"
    )

    # 6) 启动 FastAPI
    import uvicorn

    def run_api():
        uvicorn.run(
            fastapi_app,
            host=config.CALLBACK_HOST,
            port=config.CALLBACK_PORT,
            log_level="warning",
            server_header=False,
            access_log=False,
        )

    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()
    logger.info(f"API 服务已启动: http://{config.CALLBACK_HOST}:{config.CALLBACK_PORT}")
    logger.info(f"TradingView Webhook: http://{config.CALLBACK_HOST}:{config.CALLBACK_PORT}/api/tradingview/webhook")

    # 7) 优雅退出
    stop_event = threading.Event()

    def on_shutdown(signum, frame):
        logger.info("收到退出信号，正在关闭...")
        stop_event.set()

    signal.signal(signal.SIGINT, on_shutdown)
    signal.signal(signal.SIGTERM, on_shutdown)

    try:
        stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("正在清理资源...")
        if subscriber:
            subscriber.stop()
        executor.close()
        opend.stop()
        notifier.send_text("[FTSeek] 系统已停止")
        logger.info("FTSeek 已退出")


if __name__ == "__main__":
    main()
