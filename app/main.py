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
except Exception as e:
    # 注意：此时 logger 尚未初始化，使用 print 输出
    print(f"[FTSeek] Futu 日志配置失败：{e}")

from app import config
from app.strategy import STRATEGY_REGISTRY
from app.strategy.strategy_manager import strategy_manager
from app.notifier.dingtalk import DingTalkNotifier
from app.trader.executor import TradeExecutor
from app.data_subscriber import DataSubscriber
from app.server import app as fastapi_app, set_executor, set_notifier, set_strategy_manager

# ============================================================
#  模块化说明：复盘 Dashboard 已分离为独立服务
# ============================================================
# 复盘 Dashboard 现在运行在独立进程 (ftseek-review.service)
# 端口：8898 (主服务：8899)
# 日志：/opt/ftseek/FTSeek/logs/review_dashboard.log
# 
# 启动/停止复盘服务:
#   systemctl start/stop ftseek-review.service
# 
# 查看复盘服务状态:
#   systemctl status ftseek-review.service
# 
# 这样 WebSocket 错误不会影响主交易服务日志
# ============================================================

from app.risk import get_risk_manager, init_realtime_monitor

# 初始化 risk_manager 单例
risk_manager = get_risk_manager()

# ============================================================
#  日志配置 (防止重复添加 handler)
# ============================================================
def _setup_logging():
    """配置日志系统，防止 handler 重复"""
    # 使用 ftseek.main logger，避免日志重复
    logger = logging.getLogger("ftseek.main")
    
    # 只在没有handler时才添加，防止重复
    if not logger.handlers:
        # 设置日志级别
        log_level = getattr(logging, config.LOG_LEVEL, logging.INFO)
        logger.setLevel(log_level)
        
        # 日志格式
        formatter = logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
        )
        
        # 控制台处理器（输出到 stdout，由 nohup 重定向捕获）
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # 2026-04-08: 移除 FileHandler，避免与 nohup 重定向重复
        # 日志统一由 nohup >> ftseek.log 2>&1 捕获
        # file_handler = logging.FileHandler(...)
        
        # 禁止传播到父 logger，防止重复
        logger.propagate = False
    
    return logger

logger = _setup_logging()

# 【新增 2026-04-05】配置中心初始化 - 统一配置管理
try:
    from config.config_center import get_config_center
    config_center = get_config_center()

    # 启动配置热重载
    if config_center.get('features.config_hot_reload', True):
        config_center.start_auto_watch(interval=60)
        logger.info("配置热重载已启用 (每 60 秒检查变更)")

    logger.info(f"配置中心已初始化 | {config_center.get_validation_summary()}")
except Exception as e:
    logger.warning(f"配置中心初始化失败：{e}")

# 【新增 2026-04-02】初始化决策缓冲层 - 四层引擎架构核心
try:
    from app.decision_buffer import init_decision_buffer
    decision_buffer = init_decision_buffer(
        signal_cooldown=120,        # 信号冷却期（秒）
        max_pending=50,             # 最大待处理队列长度 (2026-04-08 优化: 10→30)
        min_confidence=0.35,        # 最低置信度阈值
    )
    logger.info("决策缓冲层已初始化")
except Exception as e:
    logger.warning(f"决策缓冲层初始化失败: {e}")

# 【新增 2026-04-02】配置校验 - 启动时检查配置一致性
try:
    from config.config_validator import validate_configs
    logger.info("正在执行配置校验...")
    validation_result = validate_configs()
    
    if not validation_result.is_valid:
        logger.error(f"配置校验失败: {validation_result.summary()}")
        logger.error("请修复配置错误后再启动系统")
        # 注意：这里不退出，仅警告，允许系统继续启动
    else:
        logger.info(f"✅ {validation_result.summary()}")
except Exception as e:
    logger.warning(f"配置校验失败: {e}")

# 【新增 2026-04-06】统一日志 API - 整合决策日志/失败追踪/执行质量监控
try:
    from app.logging import get_unified_logger
    unified_logger = get_unified_logger()
    logger.info("统一日志 API 已初始化")
except Exception as e:
    logger.warning(f"统一日志 API 初始化失败：{e}")
    unified_logger = None

# 【新增 2026-04-06】质量管理器初始化 - 巴菲特理念实现
# 包含：质量评分器、持仓周期管理器、资金水位管理器、止盈止损管理器、估值计算器
try:
    from app.factors import (
        get_quality_scorer,
        get_holding_period_manager,
        get_water_level_manager,
        get_profit_loss_manager,
        get_valuation_calculator,
    )

    # 初始化各管理器
    quality_scorer = get_quality_scorer()
    holding_period_manager = get_holding_period_manager()
    water_level_manager = get_water_level_manager(initial_capital=1000000)  # 100 万初始资金
    profit_loss_manager = get_profit_loss_manager(water_level_manager=water_level_manager)
    valuation_calculator = get_valuation_calculator()

    logger.info("质量管理器已初始化 (质量评分器、持仓周期管理器、资金水位管理器、止盈止损管理器、估值计算器)")
except Exception as e:
    logger.warning(f"质量管理器初始化失败：{e}")
    quality_scorer = None
    holding_period_manager = None
    water_level_manager = None
    profit_loss_manager = None
    valuation_calculator = None


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
        except Exception as e:
            logger.warning(f"检查 OpenD 进程失败：{e}")
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
#  启动持仓同步：以富途真实持仓为准，修正本地 DB
# ============================================================
def _sync_positions_from_futu(executor):
    """
    启动时从富途拉取真实持仓，与本地 DB 对齐。
    逻辑：
      1. 查富途模拟账户持仓（qty > 0 的品种）
      2. 对每个品种，用富途的 qty/cost_price 覆盖本地 OPEN 仓位记录
      3. 若本地无该品种 OPEN 记录，则新建一条（防止系统重启丢失持仓感知）
      4. 若本地存在富途已不持有的品种 OPEN 记录，将其标记为 CLOSED
      5. 同步后重算止盈止损
    
    2026-03-27 修复: 增加 OpenD 数据就绪等待重试机制
    """
    import sqlite3
    import time
    from futu import OpenSecTradeContext, TrdMarket, TrdEnv
    from app import config as _cfg
    from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem
    from datetime import datetime as _dt

    _logger = logging.getLogger("ftseek.main")

    # 2026-03-27 新增: 等待 OpenD 数据就绪
    max_retries = 12  # 最多等待 60 秒
    retry_interval = 5  # 每次等待 5 秒
    
    for attempt in range(max_retries):
        try:
            ctx = OpenSecTradeContext(
                host=_cfg.OPEND_HOST,
                port=_cfg.OPEND_PORT,
                filter_trdmarket=TrdMarket.HK
            )
            trd_env = TrdEnv.SIMULATE if _cfg.TRADE_ENV == 1 else TrdEnv.REAL
            ret, data = ctx.position_list_query(trd_env=trd_env)
            ctx.close()
            
            if ret == 0:
                break  # 成功，跳出重试
            
            # 检查是否是数据未准备好
            if "暂时还未准备好" in str(data) or "not ready" in str(data).lower():
                if attempt < max_retries - 1:
                    _logger.warning(f"[持仓同步] OpenD 数据未就绪，等待 {retry_interval} 秒后重试 ({attempt+1}/{max_retries})")
                    time.sleep(retry_interval)
                    continue
            
            # 其他错误
            _logger.warning(f"[持仓同步] position_list_query 失败: {data}")
            return
            
        except Exception as e:
            if attempt < max_retries - 1:
                _logger.warning(f"[持仓同步] 富途接口调用失败，等待重试: {e}")
                time.sleep(retry_interval)
                continue
            _logger.warning(f"[持仓同步] 富途接口调用失败: {e}")
            return
    else:
        _logger.warning("[持仓同步] 达到最大重试次数，跳过同步")
        return

    # 过滤出有效持仓（qty > 0）
    active = data[data['qty'] > 0][['code', 'qty', 'cost_price']].drop_duplicates(subset='code')
    futu_map = {row['code']: row for _, row in active.iterrows()}

    stop_pct = abs(_dem.current_params.get('stop_loss', 0.03))
    take_pct = abs(_dem.current_params.get('take_profit', 0.045))

    conn = sqlite3.connect(executor.position_manager.db_path)
    c = conn.cursor()
    now_str = _dt.now().strftime('%Y-%m-%d %H:%M:%S')

    # 查本地所有 OPEN 仓
    c.execute("SELECT id, code FROM positions WHERE status='OPEN'")
    local_open = {row[1]: row[0] for row in c.fetchall()}  # code -> id

    synced, inserted, closed_count = [], [], []

    # 1. 富途有，本地同步
    for code, row in futu_map.items():
        qty = int(row['qty'])
        cost = float(row['cost_price'])
        sl = round(cost * (1 - stop_pct), 4)
        tp = round(cost * (1 + take_pct), 4)

        if code in local_open:
            pos_id = local_open[code]
            c.execute(
                "UPDATE positions SET quantity=?, entry_price=?, stop_loss=?, take_profit=? WHERE id=?",
                (qty, cost, sl, tp, pos_id)
            )
            synced.append(f"{code}({qty}股@{cost})")
        else:
            c.execute(
                """INSERT INTO positions
                   (code, action, quantity, entry_price, entry_time, status, stop_loss, take_profit)
                   VALUES (?,?,?,?,?,'OPEN',?,?)""",
                (code, 'BUY', qty, cost, now_str, sl, tp)
            )
            inserted.append(f"{code}({qty}股@{cost})")

    # 2. 本地有，富途无 → 标记 CLOSED（富途已平仓但本地未同步）
    for code, pos_id in local_open.items():
        if code not in futu_map:
            c.execute(
                "UPDATE positions SET status='CLOSED', exit_time=? WHERE id=?",
                (now_str, pos_id)
            )
            closed_count.append(code)

    conn.commit()
    conn.close()

    if synced or inserted or closed_count:
        _logger.info(
            f"[持仓同步] 完成 | 更新: {synced or '无'} | "
            f"新增: {inserted or '无'} | 关闭幽灵仓: {closed_count or '无'}"
        )
    else:
        _logger.info("[持仓同步] 本地与富途持仓一致，无需更新")


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

    # 2.5) 启动持仓同步 - 2026-03-27 禁用
    # 改为在每小时推送交易报告前进行同步，避免启动时 OpenD 数据未就绪
    # try:
    #     _sync_positions_from_futu(executor)
    # except Exception as _sync_e:
    #     logger.warning(f"启动持仓同步失败（将继续运行）: {_sync_e}")

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

    # 【新增 2026-04-09】初始化分层策略管理器 - 三层架构核心
    try:
        from app.strategy.layered_integration import init_layered_strategy_manager
        layered_manager = init_layered_strategy_manager(config={
            'base_position': 100,  # 基础仓位
        })
        logger.info("分层策略管理器已初始化 (事件驱动/跨市场套利/技术增强)")
    except Exception as e:
        logger.warning(f"分层策略管理器初始化失败：{e}")
        layered_manager = None

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

    # 初始化交易健康度看板监控器（注入执行层/系统层依赖）
    try:
        from app.risk.risk_monitor import init_risk_monitor
        from app.risk.circuit_breaker import circuit_breaker as _cb
        _health_monitor = init_risk_monitor(
            risk_manager=risk_manager,
            dingtalk_notifier=notifier,
            circuit_breaker=_cb,
            subscriber_ref=subscriber,
            executor_ref=executor,
        )
        logger.info("交易健康度看板监控器已初始化")
    except Exception as _e:
        logger.warning(f"交易健康度看板监控器初始化失败: {_e}")

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
