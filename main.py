#!/usr/bin/env python3.11
"""
FTSeek-US 主入口
美股AI投研系统 v2.0.0 — 多模型投票 + 自适应轮询 + Futu LV3实时行情
"""
import os
import sys
import signal
import logging
import json
import time
import asyncio
from datetime import datetime

# 添加项目路径 (P2 修复: 动态路径)
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_DIR)

# 加载环境变量
from dotenv import load_dotenv
load_dotenv(os.path.join(PROJECT_DIR, '.env.us'))

# 导入配置
import config

# 全局 SQLite 安全钩子 — 必须在所有 DB 操作之前加载
# monkey-patch sqlite3.connect → 自动应用 WAL + busy_timeout
import app.core.db_utils  # noqa: F401

# ============================================================
#  日志配置
# ============================================================
os.makedirs(config.LOG_DIR, exist_ok=True)

# P2-1 修复 (2026-04-17): 不使用 FileHandler, 避免与 systemd StandardOutput=append 双重写入
# 只输出到 stdout, 由 systemd 负责写入文件
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("ftseek_us.main")


def _config_integrity_check(loader):
    """
    启动时配置完整性校验 (2026-06-14 SSOT)

    检查 YAML 中的关键 section 是否有代码消费者。
    不阻断启动, 仅 WARNING 日志。目的是让死配置可见。
    """
    import re

    try:
        yaml_path = os.path.join(PROJECT_DIR, "config", "unified_config.yaml")
        with open(yaml_path, "r") as f:
            yaml_text = f.read()

        # 扫描所有 .py 文件中 get_dotted / get() / .get(" 调用引用的 key
        code_keys = set()
        for root, dirs, files in os.walk(os.path.join(PROJECT_DIR, "app")):
            if "__pycache__" in root:
                continue
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r") as cf:
                        code_text = cf.read()
                    # 匹配 get_dotted("xxx") 和 get("xxx") 中的 key
                    for m in re.finditer(r'get_dotted\(["\']([^"\']+)', code_text):
                        code_keys.add(m.group(1))
                    for m in re.finditer(r'\.get\(["\']([^"\']+)', code_text):
                        code_keys.add(m.group(1))
                except Exception:
                    pass

        # 检查已知应被消费的关键配置
        expected_consumed = {
            "capital.fixed_amount": "fallback capital",
            "risk.position.max_per_symbol": "position limit",
            "risk.position.max_per_direction": "direction limit",
            "risk.position.max_total": "total exposure limit",
            "risk.trading.max_daily_trades": "daily trade limit",
            "risk.circuit_breaker.strategy.consecutive_losses": "strategy breaker",
            "risk.position_sizing.risk_per_trade_pct": "position sizing",
            "risk.signal_quality.min_confidence": "signal quality gate",
            "feature_flags.auto_trading_enabled": "auto trading switch",
        }

        dead = []
        for key, purpose in expected_consumed.items():
            # Check if any segment of the key appears in code
            segments = key.split(".")
            found = any(seg in code_keys for seg in segments)
            if not found:
                # Also check if the full dotted key appears
                if key not in code_keys:
                    dead.append(f"  {key} ({purpose})")

        if dead:
            logger.warning(
                f"⚠️ [ConfigIntegrity] {len(dead)} 个配置 key 可能无代码消费者:\n" +
                "\n".join(dead)
            )
        else:
            logger.info("✅ [ConfigIntegrity] 所有关键配置均有代码消费者")

    except Exception as e:
        logger.debug(f"[ConfigIntegrity] 校验跳过: {e}")


# ============================================================
#  TradingScheduler 入口 — DI 容器装配版
# ============================================================
def _run_new_scheduler(args):
    """启动 TradingScheduler: DI容器装配 + 多模型投票 + 自适应轮询 + Futu LV3"""
    import fcntl

    LOCK_FILE = "/run/ftseek-us.lock"
    try:
        lock_fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.truncate(lock_fd, 0)
        os.write(lock_fd, str(os.getpid()).encode())
    except BlockingIOError:
        try:
            with open(LOCK_FILE, 'r') as f:
                existing_pid = f.read().strip()
            print(f"❌ FTSeek-US 已在运行 (PID: {existing_pid})")
        except Exception:
            print("❌ FTSeek-US 已在运行")
        sys.exit(1)
    except Exception as e:
        print(f"⚠️  进程锁警告：{e}")

    import atexit
    def cleanup_lock():
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            os.unlink(LOCK_FILE)
        except Exception:
            pass
    atexit.register(cleanup_lock)

    # ── 启动前 OpenD 连通性检查 (P0 修复 2026-05-01) ──
    # 防止 OpenD 不可用时, Futu SDK _init_connect_sync 无限重试阻塞启动
    _opend_host = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
    _opend_port = int(os.environ.get("FUTU_OPEND_PORT", 11111))
    import socket
    _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    _sock.settimeout(5)
    try:
        _sock.connect((_opend_host, _opend_port))
        _sock.close()
        logger.info(f"[PreFlight] OpenD 连通性检查通过 ({_opend_host}:{_opend_port})")
    except Exception as _e:
        _sock.close()
        logger.critical(f"[PreFlight] ❌ OpenD 不可达 ({_opend_host}:{_opend_port}): {_e}")
        logger.critical("[PreFlight] 请先启动 Futu OpenD, 然后重试")
        sys.exit(1)

    # ── P0 修复 (2026-05-11): 模拟盘/实盘模式确认 ──
    # 当前运行阶段: 强制模拟盘 (TRADE_ENV=1)，禁止自动切换实盘
    _trade_env = os.environ.get("TRADE_ENV", "<unset, default=1>")
    _futu_trade_env = os.environ.get("FUTU_TRADE_ENV", "<unset, default=1>")
    _allow_real = os.environ.get("ALLOW_REAL_TRADING", "false").lower() == "true"
    if (_trade_env == "0" or _futu_trade_env == "0") and not _allow_real:
        logger.critical("🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨")
        logger.critical("🚨 [安全拦截] 检测到实盘模式配置但 ALLOW_REAL_TRADING!=true!")
        logger.critical("🚨 TRADE_ENV=%s | FUTU_TRADE_ENV=%s | ALLOW_REAL_TRADING=%s", _trade_env, _futu_trade_env, _allow_real)
        logger.critical("🚨 当前阶段为模拟盘验证期，禁止运行实盘交易！")
        logger.critical("🚨 如需切换实盘，请先通过 Web Dashboard 确认切换")
        logger.critical("🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨🚨")
        sys.exit(1)

    if _trade_env == "0" or _futu_trade_env == "0":
        logger.warning("[安全确认] ⚠️ 实盘模式已启用 (TRADE_ENV=%s | FUTU_TRADE_ENV=%s | ALLOW_REAL_TRADING=%s)", _trade_env, _futu_trade_env, _allow_real)
        logger.warning("[安全确认] ⚠️ 所有交易将使用真实账户，产生真实盈亏")
    else:
        logger.info("[安全确认] ✅ 当前运行模式: 模拟盘 (TRADE_ENV=%s | FUTU_TRADE_ENV=%s)", _trade_env, _futu_trade_env)
        logger.info("[安全确认] ✅ 模拟盘验证期 — 持续收集数据、验证策略")

    # ── Task #36 (2026-06-16): trade_records schema 迁移 (幂等) ──
    # 在 scheduler/trade_store 初始化前先确保 schema 完整, 防止
    # 实盘 ftseek_us_live.db 缺表/缺列时 INSERT 失败.
    try:
        from scripts.migrate_trade_records import ensure_trade_records_schema
        from app.core.paths import get_trade_records_db
        _migrate_db = get_trade_records_db()
        _mig_result = ensure_trade_records_schema(_migrate_db)
        logger.info(f"[Task#36·migration] {_mig_result}")
    except Exception as _mig_err:
        logger.warning(f"⚠️ [Task#36·migration] 失败 (降级运行): {_mig_err}")

    # ── DI 容器装配核心组件 ──
    from app.core.di_container import get_di_container
    container = get_di_container()
    scheduler = container.build_scheduler()

    def _graceful_shutdown():
        """P0 修复 (2026-04-30): 优雅关闭所有组件，避免 systemd SIGKILL timeout"""
        logger.info("[shutdown] 开始优雅关闭流程...")
        # 1. 停止 scheduler (设置 running=False, join 后台线程)
        try:
            scheduler.stop()
            logger.info("[shutdown] scheduler 已停止")
        except Exception as e:
            logger.warning(f"[shutdown] scheduler.stop() 异常: {e}")
        # 2. 停止 ConfigGov 后台线程
        for attr in ('config_drift_detector', 'config_hot_reloader'):
            obj = getattr(scheduler, attr, None)
            if obj and hasattr(obj, 'stop'):
                try:
                    obj.stop()
                except Exception:
                    pass
        # 3. P0 补齐 (2026-05-09): 关闭 EventStore SQLite 连接
        try:
            event_store.close()
            logger.info("[shutdown] EventStore 连接已关闭")
        except Exception as e:
            logger.warning(f"[shutdown] event_store.close() 异常: {e}")
        # 4. P0 补齐 (2026-05-09): 关闭 DataRouter (释放 Futu LV3 槽位)
        try:
            data_router = getattr(scheduler, 'data_router', None) or getattr(scheduler, 'market_data', None)
            if data_router and hasattr(data_router, 'close'):
                data_router.close()
                logger.info("[shutdown] DataRouter 已关闭")
        except Exception as e:
            logger.warning(f"[shutdown] data_router.close() 异常: {e}")
        # 5. P-Push (2026-05-16): 优雅关闭 PushHub (flush 聚合桶)
        try:
            from app.push import get_push_hub
            get_push_hub().stop()
            logger.info("[shutdown] PushHub 已关闭 (聚合桶已 flush)")
        except Exception as e:
            logger.warning(f"[shutdown] PushHub.stop() 异常: {e}")
        # 5.5 PostTradeHook (2026-05-16): 等异步 hook 完成
        try:
            from app.pipeline.post_trade_hook import shutdown as pth_shutdown, get_stats
            pth_shutdown(wait_sec=5)
            logger.info(f"[shutdown] PostTradeHook stats: {get_stats()}")
        except Exception as e:
            logger.warning(f"[shutdown] PostTradeHook.shutdown() 异常: {e}")
        # 6. 清理进程锁
        cleanup_lock()
        # 7. 发送关闭通知 (P2 补齐)
        try:
            notifier.send_text("[INFO] FTSeek-US 正在关闭")
        except Exception:
            pass
        # 8. 强制退出 (防止 Futu SDK 内部非 daemon 线程阻止进程退出)
        logger.info("[shutdown] 执行强制退出 (os._exit)")
        os._exit(0)

    def signal_handler(signum, frame):
        logger.info("收到停止信号，正在关闭...")
        _graceful_shutdown()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    execution_engine = container.execution_engine()
    risk_engine = container.risk_engine()
    notifier = container.notifier()
    event_store = container.event_store()

    logger.info("[DI] 核心组件已通过DI容器装配: EventStore/Execution/Risk/Notifier")

    # 【新增 Phase 5】初始化 Agent 桥接 (盘前/盘后 LLM 闭环)
    try:
        from app.llm.agent_bridge import get_agent_bridge
        agent_bridge = get_agent_bridge()
        logger.info("[Agent] 美股 Agent 桥接已初始化 | Hermes 可用=%s", agent_bridge._hermes_available)
        # 注入到 scheduler 供盘前/盘后调用
        scheduler.agent_bridge = agent_bridge
    except Exception as e:
        logger.warning(f"[Agent] 美股 Agent 桥接初始化失败 (降级模式): {e}")
        scheduler.agent_bridge = None

    # Hermes Worker 预检 (观察模式, 不阻塞启动)
    try:
        from app.llm.hermes_client import get_hermes_client
        _hc = get_hermes_client()
        if _hc.ping():
            logger.info("[HermesWorker] ✅ Worker 预检通过 (Python 3.11 subprocess)")
        else:
            logger.warning("[HermesWorker] ⚠️ Worker 预检失败 — Agent 功能将降级至 Bailian API 直连")
    except Exception as _he:
        logger.warning("[HermesWorker] ⚠️ Worker 预检异常 (降级运行): %s", _he)

    # 【新增 Phase US-1】加载美股盘前作战计划
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        
        # F18 (2026-05-12): 路径走 paths.py 单一真相源
        from app.core.paths import get_battle_plan_path
        plan_path = str(get_battle_plan_path())
        mgr = USBattlePlanManager()
        loaded = mgr.load(plan_path)
        if loaded:
            logger.info(f"✅ 美股作战计划已加载: {plan_path}")
            # F3 (2026-05-14): 强制重启校验 + 失败时 RISK_OFF
            ok = mgr.revalidate_on_restart()
            if not ok:
                logger.error(
                    "🚨 [启动·F3] battle plan 校验失败且自动修复未通过, 进入 RISK_OFF"
                )
                try:
                    from app.risk.risk_state_machine import RiskStateMachine, RiskState
                    RiskStateMachine().set_state(
                        RiskState.RISK_OFF, "stale_battle_plan_F3", "main"
                    )
                except Exception as _rse:
                    logger.warning(f"[启动·F3] RISK_OFF 设置失败 (降级): {_rse}")
        else:
            logger.warning("⚠️ 未找到美股作战计划，将使用默认策略")
    except Exception as e:
        logger.warning(f"⚠️ 加载美股作战计划失败: {e}")

    # 【P0-2 配置治理 2026-04-29 新增】三层防御机制
    # Layer 1: 启动时强制校验
    # Layer 2: 配置漂移检测 (4 小时巡检)
    # Layer 3: 热重载 (60s 冷却)
    try:
        from app.config_mgmt import ConfigValidator, ConfigDriftDetector, ConfigHotReloader

        # Layer 1: 启动时校验
        loader = config.get_config() if hasattr(config, 'get_config') else None
        if loader is None:
            from app.core.config_container import get_config as _get_loader
            loader = _get_loader()

        # P1-追加 (2026-06-16): strict_mode=True 防止 config_audit 被意外删除后
        # 配置强制校验静默消失 (双层防御: Validator + audit 均为 blocking)
        validator = ConfigValidator(loader, strict_mode=True)
        validation_result = validator.validate_all()

        if not validation_result.is_valid:
            logger.critical(
                "[ConfigGov] ❌ 配置校验失败 %d 个错误 — 实盘启动已阻止: %s",
                len(validation_result.errors),
                "; ".join(validation_result.errors[:5]),
            )
            raise RuntimeError(
                f"ConfigValidator 发现 {len(validation_result.errors)} 个阻塞错误, "
                f"请修复配置后重试"
            )
        else:
            logger.info("[ConfigGov] ✅ 配置校验通过: %s", validation_result.summary)

        # Layer 1.5: Exit Policy 启动校验 (P0-3 2026-04-29 新增)
        # exit_policy.yaml 是止损/止盈的 SSOT，启动时必须验证代码覆盖所有启用的规则
        try:
            from app.risk.exit_policy_guardian import validate_exit_policy
            ep_result = validate_exit_policy()
            if not ep_result.is_valid:
                logger.critical(
                    "[ExitPolicy] ❌ 退出策略校验失败: %s — 系统可能缺少关键止损保护!",
                    ep_result.rules_violated
                )
            else:
                logger.info(
                    "[ExitPolicy] ✅ 退出策略校验通过: %d/%d 条规则已实现 (%s)",
                    len(ep_result.rules_enabled), len(ep_result.rules_enabled), ep_result.version
                )
            if ep_result.warnings:
                for w in ep_result.warnings:
                    logger.warning("[ExitPolicy] ⚠️ %s", w)
        except Exception as e:
            logger.error("[ExitPolicy] 退出策略校验异常 (降级运行): %s", e)

        # Layer 1.6: 做空策略启动校验 (2026-04-29 新增)
        # 验证 unified_config.yaml stock_short + exit_policy.yaml short_overrides 配置完整性
        try:
            from app.execution.short_strategy import validate_short_config
            sc_result = validate_short_config()
            if not sc_result.is_valid:
                logger.critical(
                    "[ShortConfig] ❌ 做空配置校验失败: %s",
                    sc_result.errors
                )
            else:
                logger.info("[ShortConfig] ✅ 做空配置校验通过: %s", sc_result.summary)
            for w in sc_result.warnings:
                logger.warning("[ShortConfig] ⚠️ %s", w)
        except Exception as e:
            logger.error("[ShortConfig] 做空配置校验异常 (降级运行): %s", e)

        # Layer 1.7: SSOT 配置一致性审计 (YAML vs CFG 默认值, 严格模式)
        try:
            from app.core.config_audit import print_config_audit, ConfigAuditError
            _yaml_data = loader._raw_config
            _blocking = print_config_audit(_yaml_data, strict=True)
            if _blocking == 0:
                logger.info("[ConfigAudit] ✅ SSOT 配置一致性审计通过 — 无漂移检测")
        except ConfigAuditError as e:
            logger.critical("[ConfigAudit] ❌ 配置一致性审计失败 — 实盘启动已阻止: %s", e)
            raise
        except Exception as e:
            logger.warning("[ConfigAudit] 配置审计异常 (降级运行): %s", e)

        # Layer 2: 漂移检测 (4 小时)
        drift_detector = ConfigDriftDetector(loader, interval_hours=4.0)
        drift_detector.start()
        logger.info("[ConfigGov] ✅ 漂移检测已启动 (interval=4h)")

        # Layer 3: 热重载 (60s 冷却) — 同步 ConfigContainer
        def _on_config_reload():
            """热重载后同步 ConfigContainer._config_dict"""
            from app.core.config_container import ConfigContainer
            ConfigContainer().reload()
            logger.info("[HotReloader] ✅ ConfigContainer 已同步")

        hot_reloader = ConfigHotReloader(loader, cooldown_seconds=60, post_reload_callback=_on_config_reload)
        hot_reloader.start()
        logger.info("[ConfigGov] ✅ 热重载已启动 (cooldown=60s, 含ConfigContainer同步)")

        # ── P0 (2026-06-14): 启动配置完整性校验 ──
        # 检查 YAML key 是否有代码消费者, 未消费的 → WARNING
        _config_integrity_check(loader)

        # 存储引用供 scheduler 访问
        scheduler.config_validator = validator
        scheduler.config_drift_detector = drift_detector
        scheduler.config_hot_reloader = hot_reloader

    except Exception as e:
        logger.warning(f"[ConfigGov] 配置治理初始化失败 (降级运行): {e}")

    # 【新增 Phase US-2】初始化每日动态交易池 (v2 架构 2026-04-19)
    try:
        from app.utils.daily_active_pool import get_daily_active_pool
        
        pool = get_daily_active_pool()
        pool.initialize()

        # 1. 注入当前持仓 (从 trade_records.json)
        trade_records_path = os.path.join(config.BASE_DIR, 'data', 'trade_records.json')
        if os.path.exists(trade_records_path):
            with open(trade_records_path, 'r') as f:
                trades = json.loads(f.read())
            position_codes = [t['code'] for t in trades if t.get('code', '').startswith('US.')]
            if position_codes:
                pool.inject_positions(position_codes)
                logger.info(f"💼 US 注入持仓: {len(position_codes)} 只 -> {position_codes}")

        # 2. 注入盘前计划目标
        if os.path.exists(plan_path):
            with open(plan_path, 'r') as f:
                plan = json.loads(f.read())
            if plan and plan.get('targets'):
                pool.inject_plan_targets(plan)
                logger.info(f"📝 US 注入盘前计划: {len(plan['targets'])} 只")

        summary = pool.get_summary()
        logger.info(f"✅ US 动态交易池 v2 就绪: {summary['total']} 只 | {summary['breakdown']}")
    except Exception as e:
        logger.warning(f"⚠️ US 动态交易池初始化失败 (降级运行): {e}")

    logger.info("✅ 交易调度器已启动（后台线程）")
    logger.info("✅ FTSeek-US 服务已启动")
    logger.info(f"API 服务监听：http://0.0.0.0:8901  (FastAPI 待实现)")

    # P0-4 启动期健全性检查 (2026-05-19): 5/18 PostTradeHook 全天 0 触发, 验证 wiring
    # 不修源码, 仅验证关键钩点的 import + dispatch 路径不被破坏
    try:
        import importlib, inspect
        _exec_mod = importlib.import_module("app.pipeline.order_executor")
        _exec_src = inspect.getsource(_exec_mod)
        _hook_call_count = _exec_src.count("on_position_opened(")
        _hook_import_count = _exec_src.count("from app.pipeline.post_trade_hook")
        if _hook_call_count >= 2 and _hook_import_count >= 2:
            logger.info(
                f"✅ [PostTradeHook·wired] order_executor 已绑定 "
                f"({_hook_call_count} 处 on_position_opened 调用, "
                f"{_hook_import_count} 处 import) — 入场后将触发 thesis_inferer 等副作用"
            )
        else:
            logger.error(
                f"🔴 [PostTradeHook·UNWIRED] order_executor 缺少 PostTradeHook 调用! "
                f"on_position_opened 调用 {_hook_call_count} 处 (期待≥2), "
                f"import {_hook_import_count} 处 (期待≥2). "
                f"开仓后 thesis_inferer / mirror / push 全部不会触发!"
            )
    except Exception as _hke:
        logger.warning(f"⚠️ PostTradeHook wiring 自检失败 (不阻断启动): {_hke}")

    try:
        notifier.send_text("[INFO] FTSeek-US v2.0.0 (LV3实时行情) 已启动")
    except Exception:
        pass

    # 💓 盘中心跳 (2026-05-19): 30min 一次综合健康报告 → 钉钉
    # 第一性: 给决策者一眼看完的状态, 不伪造数据.
    try:
        from app.observability import intraday_heartbeat
        # 注入业务概念的"启动时间" — 优先于 psutil/proc/module_load
        if not hasattr(scheduler, "start_time") or not scheduler.start_time:
            scheduler.start_time = time.time()
        from app.observability.intraday_heartbeat import HeartbeatLoop
        _heartbeat = HeartbeatLoop(interval_min=30, scheduler=scheduler)
        _heartbeat.start()
        logger.info("💓 盘中心跳后台线程已启动 (interval=30min, 仅盘中触发)")
    except Exception as e:
        logger.warning(f"⚠️ 盘中心跳启动失败 (不影响主流程): {e}")

    # 【新增 Phase 3】启动突发拦截器后台监控
    try:
        import threading
        from app.us_pipeline.emergency_interceptor import USEmergencyInterceptor
        
        interceptor = USEmergencyInterceptor(scheduler, execution_engine)
        
        # 启动异步循环
        async def run_interceptor():
            await interceptor.start_monitoring()

        interceptor_thread = threading.Thread(target=asyncio.run, args=(run_interceptor(),), daemon=True, name="EmergencyInterceptor")
        interceptor_thread.start()
        logger.info("🛡️ 美股突发拦截器后台线程已启动")
    except Exception as e:
        logger.warning(f"⚠️ 启动突发拦截器失败: {e}")

    # 【P0 修复 2026-04-25】启动运行时持仓同步器 (Position Sync)
    # 这是消除「持仓不一致」的关键修复：
    # - 用户在 Futu App 手动开仓 → 30s 后自动同步到 PositionStore → ExitMonitor 开始监控
    # - 用户在 Futu App 手动平仓 → 30s 后自动从 PositionStore 移除 → 停止监控幽灵仓
    # - 定期校准 DB 与 Broker 的一致性
    try:
        from app.core.position_sync import start_us_position_sync

        start_us_position_sync(
            sync_interval=30,
            position_store=scheduler.position_store,  # 注入内存持仓管理器
            broker_adapter=getattr(scheduler.execution_engine, 'broker_adapter', None) if scheduler.execution_engine else None,
        )
        logger.info("✅ 运行时持仓同步器已启动 (30s 间隔, Broker→PositionStore 自动同步)")
    except Exception as e:
        logger.warning(f"⚠️ 运行时持仓同步器启动失败: {e}")

    # ── M1 (2026-05-16): 启动期强制对账 (一次性) — 多源持仓集成 ──
    # 拉 broker 实际持仓 vs 内存 → 差异自动补录, broker_only 标 LEGACY+PROTECTED
    # 与 30s position_sync 不重复: reconciler 是启动期一次性, sync 是运行时.
    # 设计原则: 默认 PROTECTED 隔离, 策略不会自动操作历史/手动仓.
    try:
        from app.positions import run_startup_reconcile, get_protection_controller
        # 初始化 ProtectionController 单例 (注入 position_store)
        get_protection_controller(position_store=scheduler.position_store)
        # 同步执行 (启动期一次性, 后续 30s sync 接力)
        _broker = getattr(scheduler.execution_engine, 'broker_adapter', None) if scheduler.execution_engine else None
        _result = run_startup_reconcile(scheduler.position_store, _broker)
        logger.info(
            f"[M1] 启动对账完成: matched={_result.matched} broker_only={_result.broker_only_added} "
            f"local_only={_result.local_only_ghosted} legacy_tagged={_result.tagged_legacy}"
        )
    except Exception as e:
        logger.warning(f"⚠️ M1 启动对账失败 (降级运行): {e}")

    # ── P1-C (2026-05-15): 关键告警通道启动检测 — 至少 1 通道可用 ──
    try:
        _has_dingtalk = bool(os.getenv("DINGTALK_WEBHOOK_URL_US", "").strip())
        _has_feishu = bool(os.getenv("FEISHU_WEBHOOK_URL_US", "").strip()) or bool(
            os.getenv("FEISHU_APP_ID", "").strip()
        )
        if not (_has_dingtalk or _has_feishu):
            logger.warning("=" * 70)
            logger.warning("⚠️  [关键告警通道] 未检测到 DingTalk/Feishu webhook!")
            logger.warning("⚠️  环境变量需配置 DINGTALK_WEBHOOK_URL_US 或 FEISHU_WEBHOOK_URL_US")
            logger.warning("⚠️  风险: RISK_OFF / KILL_SWITCH / 标的护栏触发时告警将丢失")
            logger.warning("⚠️  当前会写 data/critical_alerts.flag 文件作为兜底")
            logger.warning("=" * 70)
        else:
            channels = []
            if _has_dingtalk: channels.append("DingTalk")
            if _has_feishu: channels.append("Feishu")
            logger.info(f"[关键告警通道] ✅ 已配置: {', '.join(channels)}")
    except Exception as _alc:
        logger.debug(f"[关键告警通道] 检测异常: {_alc}")

    # ── P0-X (2026-05-15): SymbolDailyLossGuard 重启恢复当日状态 ──
    try:
        from app.risk.symbol_daily_loss_guard import get_symbol_daily_loss_guard
        try:
            from app.risk.capital_safety import get_capital_safety_system
            cs = get_capital_safety_system()
            get_symbol_daily_loss_guard().set_equity_provider(
                lambda: cs._state.total_equity or 100000
            )
        except Exception:
            pass
        get_symbol_daily_loss_guard().restore_from_db()
    except Exception as _slg:
        logger.warning(f"[启动·SymbolDailyLossGuard] 恢复失败 (降级): {_slg}")

    # ── P0-E3 (2026-05-14): OrderTracker 启动恢复 — 防止 pending 订单变孤儿 ──
    try:
        from app.risk.order_tracker import get_order_tracker
        from futu import TrdEnv as _TrdEnv
        _broker = getattr(scheduler.execution_engine, 'broker_adapter', None) if scheduler.execution_engine else None
        _trd_env = _TrdEnv.SIMULATE if int(os.getenv("TRADE_ENV", "1")) == 1 else _TrdEnv.REAL
        if _broker is not None:
            _restore_stat = get_order_tracker().restore_pending_on_startup(_broker, _trd_env)
            logger.info(f"[启动·OrderTracker] 恢复完成: {_restore_stat}")
        else:
            logger.warning("[启动·OrderTracker] broker_adapter 不可用, 跳过 pending 恢复")
    except Exception as _ote:
        logger.warning(f"[启动·OrderTracker] 恢复失败 (降级运行): {_ote}")

    # ── Task #8 (2026-06-15): ManualHoldRegistry 启动恢复 ──
    # 必须在 position_sync 启动对账完成之后、scheduler.run_forever()
    # (signal_pipeline / orb_engine 启动) 之前调用, 避免重启后 auto pipeline
    # 在首次扫描时误触手动持仓标的.
    try:
        from app.coordinator.manual_hold_registry import manual_hold_registry
        manual_hold_registry.sync_from_positions()
        _mh_count = manual_hold_registry.get_manual_count()
        _mh_symbols = manual_hold_registry.get_all_manual_symbols()
        logger.info(
            f"[启动·ManualHoldRegistry] ✅ 已恢复 {_mh_count} 个手动持仓标的: "
            f"{sorted(_mh_symbols) if _mh_symbols else '(空)'}"
        )
    except Exception as _mh_e:
        logger.warning(f"[启动·ManualHoldRegistry] 恢复失败 (降级运行): {_mh_e}")

    # ── 启动交易监控告警 (P2 2026-05-20) ──
    # 必须在 scheduler.run_forever() 之前启动，因为 run_forever 是阻塞的
    try:
        from app.core.config_container import get_config as _get_app_config
        from app.monitoring.trading_monitor import TradingMonitor, start_monitor
        
        cfg = _get_app_config()
        monitor = TradingMonitor(config=cfg.to_dict() if hasattr(cfg, 'to_dict') else {})
        monitor.start()
        logger.info("[监控告警] 已启动 (PnL/PDT/熔断器实时监控)")
    except Exception as e:
        logger.warning(f"[监控告警] 启动失败：{e}")

    # ── 启动盘中归因报告 (P2 2026-05-01) ──
    # 必须在 scheduler.run_forever() 之前启动, 因为 run_forever 是阻塞的
    try:
        from app.reports.intraday_attribution import get_intraday_attribution
        attr = get_intraday_attribution()
        attr.inject_deps(
            position_store=scheduler.position_store,
            capital_safety=getattr(scheduler, 'capital_safety', None),
            data_router=scheduler.data_router,
            exit_monitor=getattr(scheduler, 'exit_monitor', None),
        )
        attr.start()
        logger.info("[盘中归因] 已启动 (每60分钟)")
    except Exception as e:
        logger.warning(f"[盘中归因] 启动失败: {e}")

    scheduler.run_forever()


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="FTSeek-US 美股AI投研系统")
    parser.add_argument("--start", action="store_true", help="启动服务")
    parser.add_argument("--stop", action="store_true", help="停止服务")
    parser.add_argument("--status", action="store_true", help="查看状态")
    parser.add_argument("--test", action="store_true", help="测试连接")

    args = parser.parse_args()
    _run_new_scheduler(args)


if __name__ == "__main__":
    main()
