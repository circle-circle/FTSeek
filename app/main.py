"""
FTSeek 主入口
启动流程: OpenD -> 数据订阅 -> 策略引擎 -> FastAPI回调服务
"""
import sys
from app.storage.repository import get_db
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
from app.server import app as fastapi_app, set_executor, set_notifier, set_strategy_manager, set_subscriber, set_harness_pilot

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

# 初始化 risk_manager 单例 (Phase 2: time_aware_config 在 config_center 加载后更新)
risk_manager = get_risk_manager(time_aware_config=None)

# ============================================================
#  日志配置 (防止重复添加 handler)
# ============================================================
def _setup_logging():
    """配置日志系统，防止 handler 重复
    
    2026-04-09 修复 Logger 黑洞:
    之前只给 ftseek.main 配置了 handler，而 ftseek.subscriber/ftseek.consensus
    等子 logger 没有 handler，向上传播到 root logger (level=WARNING) 后
    所有 INFO 日志被静默丢弃，导致无法通过日志诊断问题。
    
    修复方案: 在 ftseek 父级 logger 上设置 handler，所有子 logger 的日志
    都能通过传播机制输出。ftseek.main 保持 propagate=False 避免重复。
    """
    log_level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
    )
    
    # 1. 配置 ftseek 父级 logger (接收所有子 logger 的传播日志)
    parent_logger = logging.getLogger("ftseek")
    if not parent_logger.handlers:
        parent_logger.setLevel(log_level)
        # P0 (2026-04-16): 使用 auto-flush handler 防止 systemd 日志缓冲
        parent_handler = logging.StreamHandler(stream=sys.stderr)
        parent_handler.setLevel(log_level)
        parent_handler.setFormatter(formatter)
        _orig_emit = parent_handler.emit
        parent_handler.emit = lambda record: (_orig_emit(record), sys.stderr.flush())
        parent_logger.addHandler(parent_handler)
        parent_logger.propagate = False  # 不再向 root 传播
    
    # 2. 配置 ftseek.main logger (主模块专用)
    logger = logging.getLogger("ftseek.main")
    if not logger.handlers:
        logger.setLevel(log_level)
        console_handler = logging.StreamHandler(stream=sys.stderr)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(formatter)
        _orig_emit = console_handler.emit
        console_handler.emit = lambda record: (_orig_emit(record), sys.stderr.flush())
        logger.addHandler(console_handler)
        # ftseek.main 有自己的 handler，禁止传播以避免重复
        logger.propagate = False
    
    return logger

logger = _setup_logging()

# 【新增 2026-04-05】配置中心初始化 - 统一配置管理
try:
    from config.config_center import get_config_center
    config_center = get_config_center()

    # 启动配置热重载
    if config_center.get('features.config_hot_reload', True):
        # 注册配置变更回调，记录变更日志以便追溯
        def _on_config_change(event):
            logger.info(
                f"[配置热更] {event.key}: {event.old_value!r} → {event.new_value!r} "
                f"(来源={event.source})"
            )
        config_center.on_change(_on_config_change)
        config_center.start_auto_watch(interval=60)
        logger.info("配置热重载已启用 (每 60 秒检查变更, 变更回调已注册)")

    logger.info(f"配置中心已初始化 | {config_center.get_validation_summary()}")

    # P1 2026-04-19 / SSOT v4.1 2026-05-06: 配置一致性审计 — 检测漂移并阻止启动
    try:
        from app.core.config_audit import print_config_audit, ConfigAuditError
        import app.config as _config_module
        _yaml_data = config_center._data  # ConfigCenter 已合并的 YAML 数据
        _blocking = print_config_audit(_yaml_data, _config_module, strict=True)
        if _blocking == 0:
            logger.info("✅ 配置一致性审计通过")
    except ConfigAuditError as _cae:
        logger.critical(f"🚨 配置一致性审计 BLOCKING 错误 — 系统拒绝启动!")
        for _err in _cae.errors:
            logger.critical(f"   {_err}")
        logger.critical("请修复 unified_config.yaml 与 config_defaults.py 之间的不一致，然后重启。")
        sys.exit(1)
    except Exception as _ca_e:
        logger.warning(f"配置一致性审计执行异常: {_ca_e}")
except Exception as e:
    logger.critical(f"🔴 配置中心初始化失败: {e} — 核心模块不可用，系统无法安全启动")
    logger.critical("请检查 config/unified_config.yaml 格式是否正确，或 config_center.py 是否正常")
    sys.exit(1)

# 【新增 2026-04-02】初始化决策缓冲层 - 四层引擎架构核心
try:
    from app.decision_buffer import init_decision_buffer
    # 从 config_center 读取统一的 min_confidence，避免与 unified_config.yaml risk_limits.min_confidence 不一致
    _min_conf = 0.45  # 2026-05-19: 提高信号门槛 (原0.35)
    try:
        _min_conf = float(config_center.get('risk_limits.min_confidence', 0.45))  # 2026-05-19: 对齐 YAML
    except Exception as _e:
        logger.warning(f'配置中心读取 min_confidence 失败: {_e}')
    
    # Phase 2: 读取分层风控配置
    _source_risk = {}
    _time_aware = {}
    try:
        _source_risk = config_center.get('risk_limits.source_aware_risk', {})
        _time_aware = config_center.get('risk_limits.time_aware_risk', {})
        # P1 修复 (2026-04-18): 更新 risk_manager 的 time_aware_config
        # 2026-05-06: SessionDetector 已废弃，迁移到 app.shared.market_hours
        if _time_aware:
            risk_manager._time_aware_enabled = True
            logger.info('risk_manager time_aware_config 已启用 (via market_hours)')
    except Exception as _e:
        logger.warning(f'配置中心读取风控配置失败: {_e}')
        
    # 从配置中心读取 max_pending (修复代码硬编码 vs unified_config.yaml 不一致)
    _max_pending = 500  # 默认值 2026-04-22: 200→500
    try:
        _max_pending = int(config_center.get('strategy.decision_buffer.max_pending', 500))
    except Exception as _e:
        logger.warning(f'配置中心读取 max_pending 失败: {_e}')
    
    decision_buffer = init_decision_buffer(
        signal_cooldown=10,
        max_pending=_max_pending,
        min_confidence=_min_conf,
        source_risk_config=_source_risk if _source_risk else None,
        time_aware_config=_time_aware if _time_aware else None
    )
    logger.info(f"决策缓冲层已初始化 | max_pending={_max_pending} | min_confidence={_min_conf}")
except Exception as e:
    logger.critical(f"🔴 决策缓冲层初始化失败: {e} — 信号过滤引擎不可用，系统无法安全启动")
    logger.critical("没有决策缓冲层，所有信号将绕过风控直接进入执行，存在资金风险")
    sys.exit(1)

# 【新增 2026-04-19 Phase HK-1】初始化全局风险状态机
try:
    from app.pipeline.risk_state_machine import RiskStateMachine
    risk_sm = RiskStateMachine()
    logger.info(f"🛡️ 全局风险状态机已初始化: {risk_sm.get_state().value}")
except Exception as e:
    logger.critical(f"🔴 全局风险状态机初始化失败: {e} — 风控门禁不可用，系统无法安全启动")
    logger.critical("没有风险状态机，交易将绕过全局风控检查（NORMAL/HALT/RISK_OFF），存在资金风险")
    sys.exit(1)

# 【新增 2026-04-19 Phase 2 | 收敛 2026-04-26】初始化开仓冷却期管理器 (TradeExitManager)
# 说明:
#   - 本模块已精简为 "冷却期专用"。
#   - 真实离场决策由 DynamicExitManager.check_exit_priority() 在 data_subscriber 中处理。
#   - 保护单有效性由 TradeExecutor 在开仓后回读 positions.stop_loss 校验。
try:
    from app.trade_exit_manager import TradeExitManager
    exit_mgr = TradeExitManager()
    exit_mgr.initialize()
    logger.info("🛡️ TradeExitManager 已初始化 (Cooldown Only)")
except Exception as e:
    logger.debug(f"TradeExitManager 初始化失败 (非核心模块，降级运行): {e}")

# 【新增 2026-04-19 Phase 2 & 4】初始化作战计划门禁 + 竞价刷新
try:
    from app.pipeline.battle_plan import BattlePlanManager
    from pathlib import Path
    
    plan_path = Path(__file__).resolve().parent.parent / 'data' / 'daily_battle_plan.json'
    battle_mgr = BattlePlanManager()
    battle_mgr.initialize(str(plan_path), enabled=True)
    
    # Phase 4: 竞价刷新 (尝试获取 ADR/期货数据验证计划)
    # 注意：这里只是尝试刷新，如果获取不到数据不影响启动
    try:
        # 构造 mock 数据或使用真实数据 (取决于 OpenD/API 状态)
        # 此时 OpenD 可能已连接，尝试获取
        market_data_snapshot = {} # Placeholder
        # battle_mgr.refresh_plan_status(market_data_snapshot) 
        # 为了安全，暂不自动运行，等待外部数据源就绪后再调用，或者在 Pipeline 中调用
    except Exception as e:
        logger.debug(f"竞价刷新跳过: {e}")
        
    logger.info(f"✅ 盘前作战计划门禁已启动: {plan_path}")
except Exception as e:
    logger.warning(f"⚠️ 作战计划门禁初始化失败: {e}")

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

# 【新增 2026-04-18】代码级配置一致性审计 (Blocking)
# 扫描所有 .py 文件中的硬编码 fallback 值，与 unified_config.yaml 比对
# 【B3-14 2026-04-26】subprocess 改 in-process：避免 Python 解释器冷启动开销
try:
    import importlib.util as _iu
    _audit_spec = _iu.spec_from_file_location(
        "_config_audit",
        "/opt/ftseek/FTSeek/scripts/config_audit.py",
    )
    _audit_mod = _iu.module_from_spec(_audit_spec)
    _audit_spec.loader.exec_module(_audit_mod)
    _audit_result = _audit_mod.audit_project("FTSeek", "/opt/ftseek/FTSeek")
    _audit_issues = _audit_result.get("issues", []) if isinstance(_audit_result, dict) else []
    if _audit_issues:
        for _item in _audit_issues:
            logger.error(f"[配置审计] {_item}")
        logger.error("⛔ 代码配置不一致! 请将代码中的硬编码值与 unified_config.yaml 对齐")
        logger.error("   运行: python3 scripts/config_audit.py 查看完整报告")
        # 注意: 不阻断启动，但强烈警告
    else:
        logger.info("✅ 代码级配置审计通过 (无硬编码不一致)")
except Exception as e:
    logger.warning(f"配置审计跳过: {e}")

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
    # SSOT: Capital Adaptive
    initial_capital = config_center.get('capital.fixed_amount', 1000000)
    water_level_manager = get_water_level_manager(initial_capital=initial_capital)
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
#  【新增 2026-05-11】ExitRuleEngine 初始化 — Phase 1 双跑模式
# ============================================================
try:
    from app.risk.exit_rule_engine import get_exit_rule_engine
    from app.risk.exit_rules import register_phase1_rules
    import yaml as _yaml
    
    engine = get_exit_rule_engine()
    register_phase1_rules(engine)
    
    # 从 YAML 加载规则配置
    _cfg_path = os.path.join(str(config.BASE_DIR), 'config', 'unified_config.yaml')
    if os.path.exists(_cfg_path):
        with open(_cfg_path, 'r') as _f:
            _full_cfg = _yaml.safe_load(_f)
        # 提取 Phase 1 规则的配置
        _exit_cfg = _full_cfg.get('dynamic_exit_params', {}).get('default', {})
        engine.configure({
            "atr_hard_stop.enabled": _exit_cfg.get('atr_stop', {}).get('enabled', True),
            "atr_hard_stop.hard_multiplier": _exit_cfg.get('atr_stop', {}).get('hard_multiplier', 1.5),
            "atr_hard_stop.min_stop_pct": _exit_cfg.get('atr_stop', {}).get('min_stop_pct', 0.02),
            "atr_hard_stop.max_stop_pct": _exit_cfg.get('atr_stop', {}).get('max_stop_pct', 0.05),
            "structural_stop.stop_loss": _exit_cfg.get('stop_loss', -0.035),
            "trailing_stop.enabled": _exit_cfg.get('trailing_stop', {}).get('enabled', True),
            "trailing_stop.trigger_profit": _exit_cfg.get('trailing_stop', {}).get('trigger_profit', 0.025),
            "trailing_stop.atr_multiplier": _exit_cfg.get('trailing_stop', {}).get('atr_multiplier', 2.5),
            "trailing_stop.trail_percent": _exit_cfg.get('trailing_stop', {}).get('trail_percent_fallback', 0.04),
        })
    
    logger.info("✅ ExitRuleEngine 已初始化 (Phase 1 双跑模式: ATR + Structural + Trailing)")
except Exception as e:
    logger.warning(f"ExitRuleEngine 初始化失败 (不影响主流程): {e}")


# ============================================================

# ============================================================
#  OpenD 管理 (由 FTSeek 服务统一编排，不独立 systemd 管理)
# ============================================================
class OpenDManager:
    """管理 OpenD 进程 (主业务服务子进程方式)
    
    2026-04-20: 支持外部管理模式 — 当环境变量 OPEND_MANAGED=external 时，
    OpenD 由独立脚本启动/停止，FTSeek 不接管其生命周期。
    """

    def __init__(self, host="127.0.0.1", port=11111):
        self.process = None
        self.host = host
        self.port = port
        self.opend_path = os.path.join(config.OPEND_DIR, "FutuOpenD")
        self.logger = logging.getLogger("ftseek.opend_manager")
        # 2026-04-20: 外部管理模式
        self.externally_managed = os.getenv("OPEND_MANAGED", "") == "external"
        if self.externally_managed:
            self.logger.info("OpenD 为外部管理 (OPEND_MANAGED=external)，FTSeek 不接管生命周期")

    def is_running(self) -> bool:
        """检查 OpenD 是否在运行 (进程 + 端口双验证)"""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "FutuOpenD"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                return False
            # 端口验证 (防止僵尸进程)
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            ret = sock.connect_ex((self.host, self.port))
            sock.close()
            return ret == 0
        except Exception as e:
            self.logger.warning(f"检查 OpenD 状态失败：{e}")
            return False

    def _kill_existing(self):
        """清理残留的 OpenD 进程"""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "FutuOpenD"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                pids = result.stdout.strip().split()
                self.logger.info(f"发现残留 OpenD 进程: {pids}，清理中...")
                for pid in pids:
                    try:
                        os.kill(int(pid), 15)  # SIGTERM
                    except ProcessLookupError:
                        pass
                time.sleep(3)
                # 强制清理
                result2 = subprocess.run(
                    ["pgrep", "-f", "FutuOpenD"],
                    capture_output=True, text=True
                )
                if result2.returncode == 0:
                    self.logger.warning("SIGTERM 无效，使用 SIGKILL 强制终止")
                    subprocess.run(["pkill", "-9", "-f", "FutuOpenD"], capture_output=True)
                    time.sleep(2)
        except Exception as e:
            self.logger.warning(f"清理残留进程失败: {e}")

    def start(self, timeout=30) -> bool:
        """启动 OpenD (带端口冲突检测和启动验证)
        
        2026-04-20: 外部管理模式下仅验证连接，不启动/不杀进程
        """
        # 2026-04-20: 外部管理模式 — 仅验证 OpenD 是否可连接
        if self.externally_managed:
            if self.is_running():
                self.logger.info("OpenD 外部管理: 验证通过，已运行")
                return True
            else:
                self.logger.error("OpenD 外部管理: OpenD 未运行！请先执行: /opt/ftseek/start_opend.sh")
                return False

        if self.is_running():
            self.logger.info("OpenD 已在运行 (进程+端口验证通过)")
            return True

        # 1. 检查二进制
        if not os.path.isfile(self.opend_path):
            self.logger.error(f"OpenD 二进制不存在: {self.opend_path}")
            return False

        # 2. 清理残留
        self._kill_existing()

        # 3. 确保端口可用
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        if sock.connect_ex((self.host, self.port)) == 0:
            self.logger.warning(f"端口 {self.port} 仍被占用，尝试强制释放")
            sock.close()
            subprocess.run(["fuser", "-k", f"{self.port}/tcp"], capture_output=True, stderr=subprocess.DEVNULL)
            time.sleep(2)
        else:
            sock.close()

        # 4. 启动
        self.logger.info(f"启动 OpenD: {self.opend_path} (端口 {self.port})")
        self.process = subprocess.Popen(
            [self.opend_path],
            cwd=config.OPEND_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # 5. 等待就绪
        for i in range(timeout):
            if self.is_running():
                self.logger.info(f"OpenD 启动成功 PID={self.process.pid} (耗时 {i}s)")
                return True
            time.sleep(1)

        self.logger.error(f"OpenD 启动超时 ({timeout}s)")
        return False

    def stop(self, timeout=15):
        """停止 OpenD
        
        2026-04-20: 外部管理模式下不杀 OpenD 进程
        """
        # 2026-04-20: 外部管理模式 — 不杀进程
        if self.externally_managed:
            self.logger.info("OpenD 外部管理: 跳过停止 (由独立脚本管理)")
            return

        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=timeout)
                self.logger.info("OpenD 已停止 (SIGTERM)")
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.logger.warning("OpenD 已强制停止 (SIGKILL)")
        elif self.is_running():
            subprocess.run(["pkill", "-TERM", "-f", "FutuOpenD"])
            time.sleep(3)
            if self.is_running():
                subprocess.run(["pkill", "-9", "-f", "FutuOpenD"])
            self.logger.info("OpenD 已停止 (外部进程)")




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

    # 【P0 修复 2026-04-26】原实现 `with get_db(...) as conn: pass` 在 with 退出后才使用 conn，
    # 连接已归还连接池，后续 cursor/commit 会作用于已释放/关闭的连接，构成死代码路径。
    # 修复：整段 I/O 语句全部纳入 with 上下文；退出时由连接池自动 commit/归还。
    with get_db(executor.position_manager.db_path) as conn:
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
        # conn.commit() 由 get_db 上下文管理器在退出时自动执行

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
    # Fix UnboundLocalError: Initialize variables for finally block
    us_monitor = None
    subscriber = None
    cross_market_scheduler = None
    premarket_scheduler = None
    hermes_processor = None
    hermes_thread = None

    logger.info("=" * 60)
    logger.info("FTSeek 启动中...")
    logger.info(f"交易环境: {'模拟' if config.TRADE_ENV == 1 else '真实'}")
    logger.info(f"港股订阅({len(config.WATCH_LIST_HK)}只): {', '.join(config.WATCH_LIST_HK)}")
    logger.info(f"美股参考({len(config.WATCH_LIST_US)}只, 不订阅实时行情): {', '.join(config.WATCH_LIST_US[:10])}{'...' if len(config.WATCH_LIST_US) > 10 else ''}")
    logger.info("=" * 60)

    # 0) 配置一致性校验 (2026-04-13 新增)
    try:
        config_issues = config.validate_dingtalk_config()
        if config_issues:
            for issue in config_issues:
                logger.warning(f"[配置校验] {issue}")
        else:
            logger.info("[配置校验] 钉钉推送配置一致 ✅")
    except Exception as e:
        logger.warning(f"[配置校验] 校验失败: {e}")

    # 1) 启动 OpenD
    opend = OpenDManager()
    if not opend.start():
        logger.warning("OpenD 未能启动，将以离线模式运行 (仅 API 服务)")

    # 2) 初始化组件 - 使用全局统一 notifier
    from app.notifier import get_dingtalk_notifier
    from app.trader.executor import set_executor_instance
    from app.trader.position_manager import PositionManager
    # 离场管理器: TradeExitManager 已在模块顶层初始化 (L200-210)
    # 离场逻辑由 data_subscriber.py 的 _check_position_exits() 内联处理
    
    notifier = get_dingtalk_notifier()


    # P3 错误告警集成 (2026-05-19): 集成飞书/钉钉告警到 ErrorHandler
    try:
        from app.core.error_alert_integration import setup_error_alerts
        setup_error_alerts(notifier=notifier)
        logger.info("[ErrorHandler] 告警集成已初始化 (钉钉通知)")
    except Exception as _alert_e:
        logger.warning(f"[ErrorHandler] 告警集成失败，仅记录日志：{_alert_e}")
    executor = TradeExecutor()
    
    # 2.5b) 初始化 FutuMarginMonitor 保证金风控 (2026-05-08)
    # 复用现有 futu_margin_monitor.py (v2.1)，包含滞回防抖/降级防护/压力测试
    try:
        from app.config import USE_MARGIN_GUARD
        if USE_MARGIN_GUARD:
            from app.risk.futu_margin_monitor import FutuMarginMonitor
            from futu import OpenSecTradeContext, TrdEnv
            
            # 创建交易上下文用于初始化保证金监控
            trd_ctx = OpenSecTradeContext(
                host=_cfg.OPEND_HOST,
                port=_cfg.OPEND_PORT,
                filter_trdmarket=TrdMarket.HK
            )
            try:
                acc_id = getattr(_cfg, 'SIMULATE_ACCOUNT_ID', '')

                executor.futu_margin_monitor = FutuMarginMonitor()
                executor.futu_margin_monitor.initialize(
                    trd_ctx=trd_ctx,
                    acc_id=acc_id,
                    enable_hysteresis=True,
                    enable_degradation=True,
                )
                logger.info("[FutuMarginMonitor] 保证金风控已启用 (滞回防抖 + 降级防护 + 压力测试)")
            finally:
                trd_ctx.close()
        else:
            logger.info("[FutuMarginMonitor] 未启用 (USE_MARGIN_GUARD=False)")
    except Exception as e:
        logger.warning(f"[FutuMarginMonitor] 初始化失败: {e}")
    
    # 2.6) 初始化每日动态交易池 (P0 修复：传入市场前缀 + 持仓对账)
    from app.utils.daily_active_pool import get_daily_active_pool
    active_pool = get_daily_active_pool(market_prefix="HK.")
    active_pool.initialize()

    # 注入持仓到动态池 (P0/P1 修复: 过滤市场 + 补充 metadata)
    try:
        db_positions = executor.position_manager.get_all_positions()
        if db_positions:
            # P0: 仅注入港股持仓
            hk_positions = [p for p in db_positions if p.code.startswith("HK.")]
            us_positions = [p for p in db_positions if p.code.startswith("US.")]

            # P1: 构建 metadata 映射
            position_map = {}
            for p in hk_positions:
                position_map[p.code] = {
                    "action": p.action,
                    "quantity": p.quantity,
                }

            active_pool.inject_positions(
                [p.code for p in hk_positions],
                position_map=position_map
            )

            if us_positions:
                logger.warning(
                    f"🚫 启动时发现 {len(us_positions)} 只美股持仓，"
                    f"已过滤不注入港股动态池: {[p.code for p in us_positions]}"
                )
    except Exception as e:
        logger.warning(f"注入持仓到动态池失败: {e}")

    # 注入盘前计划目标
    try:
        from app.pipeline.battle_plan import BattlePlanManager
        plan = BattlePlanManager()._plan
        if plan:
            active_pool.inject_plan_targets({
                "targets": plan.targets,
                "restricted": plan.restricted
            })
    except Exception as e:
        logger.warning(f"注入盘前计划到动态池失败: {e}")

    # 打印动态池状态摘要
    try:
        summary = active_pool.get_summary()
        breakdown = active_pool.get_source_breakdown()
        logger.info(
            f"✅ HK 动态交易池 v2 就绪: {summary['total']} 只 | "
            f"{breakdown}"
        )
    except Exception:
        pass
    print("✅ Executor created.")
    logger.info("✅ Executor created.")
    # exit_mgr.set_executor(executor) # 注入执行器以便紧急清仓
    
    set_executor_instance(executor) # 注册全局实例供盘前计划使用

    # 注入到 FastAPI
    set_executor(executor)
    set_notifier(notifier)

    # 2.5) 启动持仓同步 - 2026-04-22 重新启用 (修复 drift 问题)
    # 后台线程每 30 秒对比 Futu 持仓与本地 DB，自动修复幽灵仓/遗漏仓
    try:
        from app.trader.position_sync import start_position_sync
        start_position_sync(sync_interval=30, market='HK')
        # start_position_sync(sync_interval=30, market='US')  # 临时禁用，防止启动阻塞
        logger.info("✅ 持仓同步器已启动 (HK 市场, 30s 间隔, 自动修复 drift)")
    except Exception as _sync_e:
        logger.warning(f"持仓同步器启动失败（将继续运行）: {_sync_e}")

    # 2.6) 启动 Hermes Agent 处理器 (持久化后台线程)
    try:
        from app.llm.agent_processor import HermesAgentProcessor
        from app.llm.hermes_tools import ToolDependencies

        hermes_deps = ToolDependencies(
            db_path="data/ftseek.db",
            memory_dir=os.path.expanduser("~/.hermes/ftseek/memory"),
            notifier=notifier,
            config_center=config_center,
        )
        hermes_processor = HermesAgentProcessor(
            llm_config=config_center.get('hermes_agent', {}).get('llm', {}),
            deps=hermes_deps,
            polling_interval=config_center.get('hermes_agent', {}).get('polling', {}).get('interval_seconds', 5),
        )
        import threading
        hermes_thread = threading.Thread(
            target=hermes_processor.run_forever,
            name="hermes-agent-processor",
            daemon=True,
        )
        hermes_thread.start()
        logger.info("Hermes Agent 处理器已启动 (后台线程)")
    except Exception as _he:
        logger.warning(f"Hermes Agent 处理器启动失败 (将继续运行): {_he}")
        hermes_processor = None
        hermes_thread = None

    # 2.5.5) SL 合理性检查 — 修复 SL=entry 或明显不合理的止损价
    # 【2026-04-27 新增】防止手动交易或历史数据导致止损失效
    try:
        from app.risk.dynamic_exit_manager import dynamic_exit_manager as _dem_sl
        for _pos in executor.position_manager.get_all_positions():
            if _pos.status != 'OPEN':
                continue
            if _pos.stop_loss is None or _pos.stop_loss <= 0:
                # 未设置止损 → 使用默认值
                _stop_pct = abs(_dem_sl.current_params.get('stop_loss', 0.03))
                _new_sl = round(_pos.entry_price * (1 - _stop_pct), 4) if _pos.action == 'BUY' else round(_pos.entry_price * (1 + _stop_pct), 4)
                executor.position_manager.update_stop_loss_take_profit(_pos.code, _new_sl, _pos.take_profit or 0)
                logger.warning(f"[SL修复] {_pos.code} 止损未设置 → {_new_sl} (默认{_stop_pct:.0%})")
            elif _pos.stop_loss == _pos.entry_price:
                # SL=entry → 使用默认值
                _stop_pct = abs(_dem_sl.current_params.get('stop_loss', 0.03))
                _new_sl = round(_pos.entry_price * (1 - _stop_pct), 4) if _pos.action == 'BUY' else round(_pos.entry_price * (1 + _stop_pct), 4)
                executor.position_manager.update_stop_loss_take_profit(_pos.code, _new_sl, _pos.take_profit or 0)
                logger.warning(f"[SL修复] {_pos.code} SL=entry({_pos.entry_price}) → {_new_sl} (默认{_stop_pct:.0%})")
            elif _pos.action == 'BUY' and _pos.stop_loss >= _pos.entry_price:
                # 做多但 SL >= entry → 明显错误
                _stop_pct = abs(_dem_sl.current_params.get('stop_loss', 0.03))
                _new_sl = round(_pos.entry_price * (1 - _stop_pct), 4)
                executor.position_manager.update_stop_loss_take_profit(_pos.code, _new_sl, _pos.take_profit or 0)
                logger.warning(f"[SL修复] {_pos.code} SL({_pos.stop_loss})>=entry({_pos.entry_price}) → {_new_sl}")
            elif _pos.action == 'SELL' and _pos.stop_loss <= _pos.entry_price:
                # 做空但 SL <= entry → 明显错误
                _stop_pct = abs(_dem_sl.current_params.get('stop_loss', 0.03))
                _new_sl = round(_pos.entry_price * (1 + _stop_pct), 4)
                executor.position_manager.update_stop_loss_take_profit(_pos.code, _new_sl, _pos.take_profit or 0)
                logger.warning(f"[SL修复] {_pos.code} SELL SL({_pos.stop_loss})<=entry({_pos.entry_price}) → {_new_sl}")
        logger.info("✅ SL 合理性检查完成")
    except Exception as _sl_e:
        logger.warning(f"SL 合理性检查失败（将继续运行）: {_sl_e}")

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

    # 【新增 2026-04-17】初始化决策记录器 — 打通决策→交易→反哺全链路
    try:
        from app.storage.decision_recorder import get_decision_recorder
        decision_recorder = get_decision_recorder()
        logger.info("决策记录器已初始化 (DecisionRecorder)")
    except Exception as e:
        logger.warning(f"决策记录器初始化失败: {e}")
        decision_recorder = None

    # 【新增 2026-04-17】初始化自动调优器 — 基于决策记录器数据
    try:
        from app.deep_review.auto_tuner import get_auto_tuner
        auto_tuner = get_auto_tuner()
        logger.info("自动调优器已初始化 (AutoTuner)")
    except ModuleNotFoundError as e:
        logger.debug(f"自动调优器未启用 (模块未安装): {e}")
        auto_tuner = None
    except Exception as e:
        logger.warning(f"自动调优器初始化失败: {e}")
        auto_tuner = None

    # 【新增 2026-04-18】启动前持仓对账 (Reconciliation)
    # 确保本地 DB 与富途服务端持仓完全一致，修复可能的状态漂移
    # 【B3-14 2026-04-26】subprocess 改 in-process；SystemExit 视作优雅跳过
    try:
        import importlib.util as _iu
        _recon_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            '..', 'scripts', 'reconcile_positions.py',
        )
        _recon_path = os.path.abspath(_recon_path)
        if os.path.exists(_recon_path):
            logger.info("🔍 启动前持仓对账...")
            _recon_spec = _iu.spec_from_file_location("_reconcile_positions", _recon_path)
            _recon_mod = _iu.module_from_spec(_recon_spec)
            try:
                _recon_spec.loader.exec_module(_recon_mod)
                if hasattr(_recon_mod, 'run_reconciliation'):
                    _recon_mod.run_reconciliation('HK')
                    logger.info("✅ 持仓对账完成")
                else:
                    logger.warning("⚠️ reconcile_positions 模块缺少 run_reconciliation")
            except SystemExit as _se:
                # futu 不可用时脚本顶部会 sys.exit(0) — 视作优雅跳过
                logger.info(f"持仓对账优雅跳过 (exit={_se.code})")
    except Exception as e:
        logger.warning(f"⚠️ 持仓对账失败 (不影响启动): {e}")

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

    # 【新增 2026-04-14】初始化跨市场套利调度器 - 开盘接力方案核心
    try:
        from app.strategy.cross_market import get_cross_market_strategy
        from app.strategy.cross_market_scheduler import init_scheduler
        from app.strategy.transition_manager import init_transition_manager
        
        # 初始化过渡管理器
        transition_mgr = init_transition_manager()
        logger.info("过渡管理器已初始化 (跨市场→趋势策略主导权交接)")
        
        # 获取跨市场策略实例
        cross_market_strategy = get_cross_market_strategy()
        
        # 【新增 2026-04-11 Task9】初始化 LLM 分析器（供调度器 09:25 预分析使用）
        _llm_analyst_for_scheduler = None
        try:
            from app.ai.llm_market_analyst import LLMMarketAnalyst
            _llm_config = config.AI.get('llm_market_analyst', {}) if hasattr(config, 'AI') else {}
            if not _llm_config:
                # 从 config_center 读取
                try:
                    _llm_config = config_center.get('ai.llm_market_analyst', {}) or {}
                except Exception:
                    _llm_config = {}
            if _llm_config.get('enabled', False):
                _llm_analyst_for_scheduler = LLMMarketAnalyst(_llm_config)
                logger.info("LLM分析器已初始化（调度器预分析）: model=%s", _llm_config.get('model', ''))
            else:
                logger.info("LLM分析器未启用（llm_market_analyst.enabled=false），跳过调度器预分析")
        except Exception as _llm_e:
            logger.warning(f"LLM分析器初始化失败（调度器将以无LLM模式运行）: {_llm_e}")
        
        # 初始化调度器 (默认自动执行模式)
        cross_market_scheduler = init_scheduler(
            strategy=cross_market_strategy,
            notifier=notifier,
            trade_executor=executor,  # 可选,用于实际执行
            llm_analyst=_llm_analyst_for_scheduler,  # Task9: 注入LLM分析器
        )
        
        # 启动调度器 (后台线程运行)
        cross_market_scheduler.start()
        logger.info("跨市场套利调度器已启动 (09:00分析/09:10推送/09:25LLM预分析/09:30执行/10:00清理)")
        
    except Exception as e:
        logger.warning(f"跨市场套利调度器初始化失败：{e}")
        cross_market_scheduler = None
    
    # 【新增 2026-04-12 Phase 1.3】初始化美股隔夜监控器
    if config_center.get('base.market', 'HK') != 'HK':
        try:
            from app.strategy.us_overnight_monitor import init_monitor
            us_monitor = init_monitor(notifier)
            us_monitor.start()
            logger.info("美股隔夜监控器已启动 (21:00-06:00)")
        except Exception as e:
            logger.warning(f"美股隔夜监控器初始化失败：{e}")
            us_monitor = None

    # 【新增 2026-04-12】初始化盘前 Pipeline 调度器 (新闻→事件包→LLM分析→交易路由)
    premarket_scheduler = None
    try:
        from app.pipeline.premarket_scheduler import PreMarketScheduler

        pm_config = config_center.get('premarket_pipeline', {}) or {}
        if hasattr(pm_config, 'to_dict'):
            pm_config = pm_config.to_dict()

        if pm_config.get('enabled', True):
            schedule_times = pm_config.get('schedule', {}).get('times', ['08:30', '09:00'])
            premarket_scheduler = PreMarketScheduler(
                pipeline_config=pm_config,
                schedule_times=schedule_times,
            )
            premarket_scheduler.start()
            logger.info(
                f"盘前 Pipeline 调度器已启动: schedule={schedule_times}"
            )
        else:
            logger.info("盘前 Pipeline 未启用 (premarket_pipeline.enabled=false)")
    except Exception as e:
        logger.warning(f"盘前 Pipeline 调度器初始化失败：{e}")
        premarket_scheduler = None

    # ── P2 2026-05-06: 盘后 EOD 复盘调度 — 已迁移到 Hermes cronjob (16:30 HKT) ──
    # APScheduler 的 16:15 任务与 Hermes cronjob 16:30 重复，已禁用
    eod_scheduler = None
    logger.info("盘后 EOD 复盘调度已迁移到 Hermes cronjob (16:30 HKT)")

    # ── P2 2026-05-06: 午间轻量归因调度 — 已迁移到 Hermes cronjob (12:15 HKT) ──
    # APScheduler 的 12:15 任务与 Hermes cronjob 12:15 完全重复，已禁用
    midday_scheduler = None
    logger.info("午间轻量归因调度已迁移到 Hermes cronjob (12:15 HKT)")

    # ── 午间作战计划刷新调度器 (12:00 HKT) — job 在 subscriber 就绪后添加
    midday_refresher_scheduler = None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler as _BS2
        from app.pipeline.midday_plan_refresher import MiddayPlanRefresher
        midday_refresher_scheduler = _BS2(timezone='Asia/Hong_Kong')
        midday_refresher_scheduler.start()
        logger.info("午间作战计划刷新调度器已创建 (job 待 subscriber 就绪)")
    except Exception as e:
        logger.warning(f"午间作战计划刷新调度器创建失败: {e}")

    # ── 新增 (2026-05-06): 盘前多模型辩论调度 (08:20 HKT) ──
    premarket_debate_scheduler = None
    try:
        from apscheduler.schedulers.background import BackgroundScheduler as _BS3
        from app.pipeline.premarket_debate_scheduler import PremarketDebateScheduler
        premarket_debate_scheduler = _BS3(timezone='Asia/Hong_Kong')
        debate_instance = PremarketDebateScheduler(enabled=True)
        premarket_debate_scheduler.add_job(
            debate_instance.run, 'cron',
            hour=8, minute=20, day_of_week='mon-fri',
            id='hk-premarket-debate',
        )
        premarket_debate_scheduler.start()
        logger.info("✅ 盘前多模型辩论调度已启动 (08:20 HKT, Mon-Fri)")
    except Exception as e:
        logger.warning(f"盘前多模型辩论调度启动失败: {e}")

    # ── 新增：盘前计划执行器调度 ──
    battle_plan_service = None
    try:
        from app.execution.battle_plan_executor import BattlePlanExecutionService
        battle_plan_service = BattlePlanExecutionService()
        # 启动后台线程定期检查执行窗口 (09:20 - 09:35)
        import threading
        def _battle_plan_loop():
            while True:
                try:
                    time.sleep(60) # 每分钟检查一次
                    battle_plan_service.run_daily_check()
                except Exception as e:
                    logger.error(f"盘前计划执行检查异常: {e}")
        
        threading.Thread(target=_battle_plan_loop, daemon=True, name="BattlePlanLoop").start()
        logger.info("✅ 盘前计划执行服务已启动 (09:20-09:35 自动触发)")
    except Exception as e:
        logger.warning(f"盘前计划执行服务初始化失败：{e}")
        battle_plan_service = None
    # ──────────────────────────────

    # 获取已启用的策略列表供数据订阅使用
    strategies = strategy_manager.enabled()
    enabled_names = [s.name for s in strategies]
    all_names = [s.name for s in strategy_manager.all()]
    logger.info(f"策略已加载: {all_names}")
    logger.info(f"策略已启用: {enabled_names}")

    # 【P0 修复 2026-04-26】无条件提前启动 API（旧实现在 opend_running 分支内启一次，
    # 后续又在函数末尾重复启动一次 → 同端口双 uvicorn）。
    # API 服务在 offline / 纯 API 模式下也需要运行，故提到 if opend_running 之前统一启动。
    import uvicorn as _uvicorn
    import threading as _threading

    def _run_api():
        _uvicorn.run(
            "app.server:app",
            host=config.CALLBACK_HOST,
            port=config.CALLBACK_PORT,
            workers=1,
            log_level="warning",
            server_header=False,
            access_log=False,
        )

    _api_thread = _threading.Thread(target=_run_api, daemon=True)
    _api_thread.start()
    logger.info(f"✅ API 服务已启动：http://{config.CALLBACK_HOST}:{config.CALLBACK_PORT} (单入口, workers=1)")
    logger.info(f"TradingView Webhook: http://{config.CALLBACK_HOST}:{config.CALLBACK_PORT}/api/tradingview/webhook")

    # 4) 启动数据订阅 (如果 OpenD 在运行)
    subscriber = None
    opend_running = opend.is_running()
    logger.info(f"OpenD 运行状态检查：{opend_running}")
    if opend_running:
        try:
            logger.info("正在创建 DataSubscriber...")
            # 【修复 2026-04-11】传递LLM分析师到DataSubscriber,使共识引擎能够读取LLM缓存
            # 【P0 2026-04-19】注入统一 TradeExecutor 实例，消除双实例导致的风控状态不一致

            # Phase 12A: DI Container 组装 (feature flag)
            # Phase 20B: 默认启用 DI Container (从 'false' 改为 'true')
            _use_assembler = os.environ.get('FTSEEK_USE_ENGINE_ASSEMBLER', 'true').lower() == 'true'
            if _use_assembler:
                try:
                    from app.core.engine_assembler import get_assembler
                    assembler = get_assembler(config=config)
                    subscriber = assembler.assemble_subscriber(
                        strategies=strategies,
                        notifier=notifier,
                        llm_analyst=_llm_analyst_for_scheduler,
                        trade_executor=executor,
                    )
                    logger.info("[Phase 12A] DataSubscriber 已通过 EngineAssembler 组装")
                except Exception as asm_err:
                    logger.error(f"[Phase 12A] EngineAssembler 失败，回退到手动组装: {asm_err}")
                    subscriber = DataSubscriber(strategies, notifier, llm_analyst=_llm_analyst_for_scheduler, trade_executor=executor)
            else:
                subscriber = DataSubscriber(strategies, notifier, llm_analyst=_llm_analyst_for_scheduler, trade_executor=executor)

            # P0 (2026-04-16): 注入到 FastAPI server，供新闻事件注入接口使用
            set_subscriber(subscriber)
            logger.info("DataSubscriber 创建成功，正在启动订阅...")
            subscriber.start()
            logger.info("DataSubscriber.start() 完成")

            # P2 状态恢复 (2026-05-19): 从磁盘恢复交易状态
            try:
                from app.services.trading_state_manager import get_trading_state_manager
                state_mgr = get_trading_state_manager()
                state_path = "/opt/ftseek/FTSeek/data/trading_state.json"
                if state_mgr.restore(state_path):
                    logger.info(f"[TradingStateManager] 状态已从 {state_path} 恢复")
                else:
                    logger.info("[TradingStateManager] 无历史状态，从空状态开始")
            except Exception as _state_e:
                logger.warning(f"[TradingStateManager] 状态恢复失败，从空状态开始：{_state_e}")

            # 注入 quote_ctx 到 Hermes Agent (subscriber 就绪后)
            if hermes_processor and getattr(subscriber, '_quote_ctx', None):
                hermes_processor.deps.quote_ctx = subscriber._quote_ctx
                logger.info("Hermes Agent quote_ctx 已注入")
        except Exception as e:
            logger.error(f"数据订阅启动失败: {e}")
            logger.info("将以纯 API 模式运行")

    # 午间作战计划刷新调度 (12:00 HKT) — subscriber 就绪后启动
    if subscriber and getattr(subscriber, '_quote_ctx', None):
        try:
            from app.pipeline.midday_plan_refresher import MiddayPlanRefresher
            refresher = MiddayPlanRefresher(subscriber=subscriber, quote_ctx=subscriber._quote_ctx)
            midday_refresher_scheduler.add_job(
                refresher.run, 'cron', hour=12, minute=0, day_of_week='mon-fri',
                id='hk-midday-plan-refresh'
            )
            logger.info("午间作战计划刷新已激活 (12:00 HKT, Mon-Fri)")
        except Exception as e:
            logger.warning(f"午间作战计划刷新激活失败: {e}")

    # ── 2026-05-11 新增: 盘中异动扫描器 (每 30 分钟) ──
    intraday_scanner_scheduler = None
    if subscriber and getattr(subscriber, '_quote_ctx', None):
        try:
            from apscheduler.schedulers.background import BackgroundScheduler as _BS4
            from app.pipeline.intraday_alert_scanner import get_intraday_scanner
            
            intraday_scanner_scheduler = _BS4(timezone='Asia/Hong_Kong')
            intraday_scanner_scheduler.start()
            
            scanner = get_intraday_scanner(quote_ctx=subscriber._quote_ctx)
            
            def _run_intraday_scan():
                """盘中异动扫描调度入口"""
                try:
                    alerts = scanner.scan()
                    if alerts:
                        added = scanner.add_to_pool(alerts)
                        # 触发订阅同步，让 DataSubscriber 开始 Push 异动标的
                        if added and hasattr(subscriber, '_sync_subscription'):
                            subscriber._sync_subscription()
                except Exception as e:
                    logger.error(f"[盘中异动扫描] 异常: {e}")
            
            # 交易时段: 09:30-12:00, 13:00-16:00, 每 30 分钟
            intraday_scanner_scheduler.add_job(
                _run_intraday_scan, 'cron',
                hour='9-11,13-15', minute='0,30', day_of_week='mon-fri',
                id='hk-intraday-alert-scan',
            )
            logger.info("✅ 盘中异动扫描器已启动 (09:30-16:00 HKT, 每 30 分钟)")
        except Exception as e:
            logger.warning(f"盘中异动扫描器启动失败: {e}")

    # ── 新增 (2026-05-18): 盘中实时诊断 (每30分钟) — Layer 1 ──
    intraday_diag_scheduler = None
    if subscriber:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler as _BS_Diag
            from app.diagnosis.intraday_diagnoser import get_intraday_diagnoser
            
            intraday_diag_scheduler = _BS_Diag(timezone='Asia/Hong_Kong')
            intraday_diag_scheduler.start()
            
            diagnoser = get_intraday_diagnoser(notifier=notifier)
            
            def _run_intraday_diagnosis():
                """盘中诊断调度入口"""
                try:
                    alerts = diagnoser.run_diagnosis()
                    if alerts:
                        logger.info(f"[盘中诊断] 发现 {len(alerts)} 条警报")
                except Exception as e:
                    logger.error(f"[盘中诊断] 异常: {e}")
            
            # 交易时段: 09:30-12:00, 13:00-16:00, 每30分钟
            # 延迟5分钟启动，等待系统稳定
            intraday_diag_scheduler.add_job(
                _run_intraday_diagnosis, 'cron',
                hour='9-11,13-15', minute='5,35', day_of_week='mon-fri',
                id='hk-intraday-diagnosis',
            )
            logger.info("✅ 盘中实时诊断已启动 (09:35-15:35 HKT, 每30分钟)")
        except Exception as e:
            logger.warning(f"盘中实时诊断启动失败: {e}")

    # 初始化交易健康度看板监控器（注入执行层/系统层依赖）
    _harness_pilot = None
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

    # 【Harness Pilot 2026-05-02】启动 Harness 整车平台试点
    # 包裹现有系统，提供盘后复盘/风控检查等非交易工作流
    # 不触碰交易路径，独立 event loop 运行
    try:
        from app.harness_pilot import HarnessPilot
        _harness_pilot = HarnessPilot(
            data_subscriber=subscriber,
            risk_manager=risk_manager,
            notifier=notifier,
        )
        _harness_pilot.start()
        set_harness_pilot(_harness_pilot)
        logger.info("HarnessPilot 已启动（盘后复盘/风控检查后台协调器）")

        # 周末元分析调度: 从 HarnessPilot 内部调度迁移至 APScheduler 统一管理
        try:
            from apscheduler.schedulers.background import BackgroundScheduler as _BS_Weekly
            _weekly_meta_scheduler = _BS_Weekly(timezone='Asia/Hong_Kong')
            _weekly_meta_scheduler.add_job(
                _harness_pilot.run_weekly_meta_sync,
                'cron', day_of_week='sat', hour=10, minute=0,
                id='weekly-meta-analysis',
            )
            _weekly_meta_scheduler.start()
            logger.info("周末元分析调度已迁移至 APScheduler (周六 10:00 HKT)")
        except Exception as _e:
            logger.warning(f"周末元分析调度迁移失败: {_e}")
    except Exception as _e:
        logger.warning(f"HarnessPilot 启动失败: {_e}（不影响交易系统运行）")

    # 【Domain Events 2026-05-04】注册领域事件插件
    # Phase 20A: 插件自动发现 (自动注册 app/plugins/ 下所有插件)
    try:
        from app.plugins import discover_plugins
        _registered = discover_plugins()
        logger.info(f"[插件系统] 自动发现完成，已注册 {len(_registered)} 个插件: {', '.join(_registered)}")
    except Exception as _e:
        logger.warning(f"[插件系统] 自动发现失败，回退手动注册: {_e}")
        # 回退到手动注册
        try:
            from app.core.domain_events import get_event_bus
            from app.plugins.obsidian_plugin import ObsidianPlugin
            _plugin_enabled = getattr(config, "OBSIDIAN_PLUGIN_ENABLED", True)
            _obsidian_plugin = ObsidianPlugin(enabled=_plugin_enabled)
            _obsidian_plugin.register(get_event_bus())
            logger.info(f"ObsidianPlugin 已注册 (enabled={_plugin_enabled})")
        except Exception as _e2:
            logger.debug(f"ObsidianPlugin 注册跳过: {_e2}")

        try:
            from app.plugins.risk_officer_plugin import RiskOfficerPlugin
            _risk_officer_plugin = RiskOfficerPlugin(enabled=True)
            _risk_officer_plugin.register(get_event_bus())
            if risk_manager is not None:
                _risk_officer_plugin.set_risk_manager(risk_manager)
            logger.info("RiskOfficerPlugin 已注册 (自适应风控)")
        except Exception as _e2:
            logger.debug(f"RiskOfficerPlugin 注册跳过: {_e2}")

    # 5) 发送启动通知
    strategy_summary = "\n".join(
        f"  {'✅' if s.enabled else '❌'} {s.name}"
        for s in strategy_manager.all()
    )
    # 港股美股分开显示，明确哪些参与实时订阅
    _hk_count = len(config.WATCH_LIST_HK)
    _us_count = len(config.WATCH_LIST_US)
    _hk_list = ", ".join(config.WATCH_LIST_HK[:10])
    if len(config.WATCH_LIST_HK) > 10:
        _hk_list += f" ...等{_hk_count}只"
    _us_list = ", ".join(config.WATCH_LIST_US[:8])
    if len(config.WATCH_LIST_US) > 8:
        _us_list += f" ...等{_us_count}只"
    notifier.send_text(
        f"[FTSeek] 系统已启动\n"
        f"环境: {'模拟' if config.TRADE_ENV == 1 else '真实'}\n"
        f"港股订阅(实时行情): {_hk_count}只 — {_hk_list}\n"
        f"美股参考(不订阅实时行情): {_us_count}只 — {_us_list}\n"
        f"策略:\n{strategy_summary}\n"
        f"回调: {config.CALLBACK_EXTERNAL_URL}\n"
        f"TradingView Webhook: {config.CALLBACK_EXTERNAL_URL}/api/tradingview/webhook"
    )

    # 6) FastAPI 已在第 4) 步永久性启动（单入口）；此处不再重复启动。
    # 【P0 修复 2026-04-26】移除同端口的第二次 uvicorn.run，解决双进程绑定冲突。

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
        

        # P2 状态持久化 (2026-05-19): 关闭前持久化交易状态
        try:
            from app.services.trading_state_manager import get_trading_state_manager
            state_mgr = get_trading_state_manager()
            state_path = "/opt/ftseek/FTSeek/data/trading_state.json"
            if state_mgr.persist(state_path):
                logger.info(f"[TradingStateManager] 状态已持久化到 {state_path}")
            else:
                logger.warning("[TradingStateManager] 状态持久化失败")
        except Exception as _state_e:
            logger.warning(f"[TradingStateManager] 状态持久化失败：{_state_e}")
        # 2026-05-13 修复: shutdown 串行链路加超时保护，防止单步阻塞拖垮全局
        import contextlib
        
        # 1. 停止数据订阅 (含 Futu API 退订)
        if subscriber:
            with contextlib.suppress(Exception):
                subscriber.stop()
        
        # 2. 停止跨市场套利调度器
        if cross_market_scheduler:
            with contextlib.suppress(Exception):
                cross_market_scheduler.stop()
        
        # 3. 停止美股隔夜监控器
        if us_monitor:
            with contextlib.suppress(Exception):
                us_monitor.stop()
        
        # 4. 停止午间刷新调度 (wait=False 已内置超时)
        if midday_refresher_scheduler:
            with contextlib.suppress(Exception):
                midday_refresher_scheduler.shutdown(wait=False)
        
        # 5. 停止盘中异动扫描器
        if intraday_scanner_scheduler:
            with contextlib.suppress(Exception):
                intraday_scanner_scheduler.shutdown(wait=False)
        
        # 5.5. 停止盘中诊断调度器
        if intraday_diag_scheduler:
            with contextlib.suppress(Exception):
                intraday_diag_scheduler.shutdown(wait=False)
        
        # 6. 停止盘前多模型辩论调度
        if premarket_debate_scheduler:
            with contextlib.suppress(Exception):
                premarket_debate_scheduler.shutdown(wait=False)
        
        # 7. 停止盘前 Pipeline 调度器
        if premarket_scheduler:
            with contextlib.suppress(Exception):
                premarket_scheduler.stop()
        
        # 8. 停止 Hermes Agent 处理器 (join 已有 5s 超时)
        if hermes_processor:
            with contextlib.suppress(Exception):
                hermes_processor.stop()
        if hermes_thread:
            hermes_thread.join(timeout=5)
        
        # 9. 停止 HarnessPilot
        if _harness_pilot:
            with contextlib.suppress(Exception):
                _harness_pilot.stop()
        
        # 10. 关闭执行器和 OpenD
        with contextlib.suppress(Exception):
            executor.close()
        with contextlib.suppress(Exception):
            opend.stop()
        
        # 11. 通知 — 网络调用可能挂起，超时保护
        with contextlib.suppress(Exception):
            notifier.send_text("[FTSeek] 系统已停止")
        
        logger.info("FTSeek 已退出")


if __name__ == "__main__":
    main()
