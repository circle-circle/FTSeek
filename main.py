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
# 格式契约 (2026-09-10 加 threadName 时实测全部下游解析面, 改格式前必读):
#   ① asctime 必须占据行首前 19 字符 — daily_alert_summary.parse_log_alerts 取 line[:19],
#      intraday_heartbeat 取 line[:16], error_alert.py 取 line[:16] (2026-09-10 .sh→.py 重写后
#      同口径; 旧 .sh 的 awk 取 $1" "$2 且对无时间戳行做字符串比较 = 恒判"最近"), startup_smoke_check
#      用 re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}") 定位启动段。
#   ② "] %(levelname)s:" 必须紧邻 — intraday_heartbeat.py:244 与 scripts/error_alert.py 的
#      LEVEL_MARKERS 都是**字面量**匹配 "] ERROR:" / "] WARNING:" / "] CRITICAL:"。在
#      [%(name)s] 与级别之间插任何字段 → 两个守卫同时恒报 0 错 0 警,
#      即"守卫只能报健康"失效形态 (同源教训: ops/hermes/ftseek_us_restart_20260829_hotfix.sh
#      首版写 '/ ERROR /' 而日志形态是 "] ERROR:")。
#   ③ 故 threadName 只能插在 asctime 与 [%(name)s] 之间。多线程 (scan/monitor/出场快循环/
#      LLM循环/心跳/对账) 共写一条 stdout, 无此字段时交错行无法归因。
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(threadName)s] [%(name)s] %(levelname)s: %(message)s",
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
def _lock_file_candidates():
    """DEB-20260904 #140: 锁文件候选路径 — 启动与 --stop/--status 共用同一解析,
    防止启动回退数据目录锁后 stop/status 找不到运行中实例"""
    from pathlib import Path as _Plock
    candidates = ["/run/ftseek-us.lock"]
    try:
        from app.core.paths import get_data_dir as _gdd
        candidates.append(str(_Plock(str(_gdd())) / ".ftseek-us.lock"))
    except Exception as _e:
        print(f"⚠️  数据目录锁候选构建失败, 仅用 /run 锁: {_e}", file=sys.stderr)
    return candidates


def _resolve_lock_file():
    """DEB-20260904 #140: 按启动同序解析实际锁文件 — 优先含存活 PID 的候选,
    其次任一已存在候选, 兜底默认 /run 路径 (保持'未运行'语义)"""
    candidates = _lock_file_candidates()
    first_existing = None
    for cand in candidates:
        if not os.path.exists(cand):
            continue
        if first_existing is None:
            first_existing = cand
        try:
            with open(cand, "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            return cand  # 存活实例持有的锁
        except Exception as _e:
            logger.debug("锁候选 %s 无存活进程: %s", cand, _e)
            continue
    return first_existing or candidates[0]


def _validate_startup_mode_or_exit():
    """在任何 OpenD/DI 副作用前阻断 broker、数据库路由或许可半翻。"""
    from app.core.trading_mode import (
        read_startup_mode_snapshot,
        validate_startup_mode_config,
    )

    snapshot = read_startup_mode_snapshot()
    result = validate_startup_mode_config(
        os.getenv("AUTO_TRADE_ACCOUNT", snapshot["AUTO_TRADE_ACCOUNT"]),
        snapshot["TRADE_ENV"],
        os.getenv("ALLOW_REAL_TRADING", snapshot["ALLOW_REAL_TRADING"]),
        os.getenv("FTSEEK_DB_FILE", snapshot["FTSEEK_DB_FILE"]),
    )
    deprecated_futu_env = os.getenv(
        "FUTU_TRADE_ENV", snapshot["FUTU_TRADE_ENV"]
    ).strip()
    if deprecated_futu_env:
        logger.warning(
            "[StartupMode] FUTU_TRADE_ENV=%s 已废弃，不参与 broker/DB 模式判定",
            deprecated_futu_env,
        )
    if not result["valid"]:
        for error in result["errors"]:
            logger.critical("[StartupMode] %s: %s", error["code"], error["message"])
        raise SystemExit(1)

    legacy_global_flip = (
        result["broker_route"] == "real" or result["db_route"] == "live"
    )
    if legacy_global_flip:
        legacy_override = os.getenv(
            "FTSEEK_ALLOW_LEGACY_GLOBAL_FLIP", ""
        ).strip().lower() == "1"
        if not legacy_override:
            message = (
                "检测到旧全局 REAL/TRADE_ENV 翻转。RFC-131 后 LIVE 必须走 "
                "playbook.live_manual_arm 显式双臂，SIM 必须常驻；启动被拒绝。"
            )
            logger.critical("[StartupMode] LEGACY_GLOBAL_FLIP_BLOCKED: %s", message)
            try:
                from app.push import Level, push_text_safe
                push_text_safe(
                    f"🚨 [启动阻断] 旧全局实盘翻转\n{message}",
                    level=Level.HIGH,
                    title="旧全局实盘翻转被阻断",
                    sync=True,
                )
            except Exception as push_error:
                logger.warning("[StartupMode] 阻断通知发送失败: %s", push_error, exc_info=True)
            raise SystemExit(1)
        logger.warning(
            "[StartupMode] LEGACY_GLOBAL_FLIP_OVERRIDE=1: 允许旧全局模式启动（仅限显式测试/回滚）"
        )

    logger.info(
        "[StartupMode] 模式一致: broker=%s db=%s ALLOW_REAL_TRADING=%s",
        result["broker_route"], result["db_route"], result["allow_real_trading"],
    )
    return result


def _exit_policy_startup_gate(live_capable: bool) -> None:
    """Layer 1.5: Exit Policy 启动校验 (P0-3 2026-04-29; 2026-10-08 加 LIVE fail-close).

    exit_policy.yaml 是止损/止盈的 SSOT，启动时必须验证代码覆盖所有启用的规则。
    SIM (纯研究) 校验失败保持告警降级; LIVE-capable (broker/db 任一 real 或
    ALLOW_REAL_TRADING=true, 含已授权工程探针窗口) 下校验失败/异常 → SystemExit
    拒绝启动 —— 原实现只打 CRITICAL 继续跑, 对允许真钱的模式等于"止损保护
    可能缺失但照常开仓"。
    """
    try:
        from app.risk.exit_policy_guardian import validate_exit_policy
        ep_result = validate_exit_policy()
        if not ep_result.is_valid:
            logger.critical(
                "[ExitPolicy] ❌ 退出策略校验失败: %s — 系统可能缺少关键止损保护!",
                ep_result.rules_violated
            )
            if live_capable:
                raise SystemExit(
                    f"[StartupMode] LIVE-capable 模式下 ExitPolicy 校验失败 — "
                    f"规则未实现: {ep_result.rules_violated}; 拒绝启动 (exit=1)"
                )
        else:
            logger.info(
                "[ExitPolicy] ✅ 退出策略校验通过: %d/%d 条规则已实现 (%s)",
                len(ep_result.rules_enabled), len(ep_result.rules_enabled), ep_result.version
            )
        if ep_result.warnings:
            for warning in ep_result.warnings:
                logger.warning("[ExitPolicy] ⚠️ %s", warning)
    except SystemExit:
        raise
    except Exception as exc:
        logger.error("[ExitPolicy] 退出策略校验异常 (降级运行): %s", exc, exc_info=True)
        if live_capable:
            raise SystemExit(
                f"[StartupMode] LIVE-capable 模式下 ExitPolicy 校验异常: {exc}; "
                "拒绝启动 (exit=1)"
            )


def _config_drift_alert(diff: dict) -> None:
    """ConfigDriftDetector 回调: 外部改动配置 (非热重载) 必须让值班端看见。

    2026-10-17 审计修复: 检测器有 on_drift_detected 参数, 但 main.py 构造时没传 ⇒
    漂移只写 ~/.hermes 本地日志 + WARNING, 推送侧零感知 (外部改动可能一直不生效
    直到下次重启)。走 PushHub (HIGH/SYSTEM), dedup 用 hash 对, 与 detector 漂移后
    自动重快照配合 ⇒ 同一漂移不反复推送。
    """
    old_hash = diff.get("old_hash", "?")
    new_hash = diff.get("new_hash", "?")
    try:
        from app.push import get_push_hub
        from app.push.message import PushMessage, Level, Category
        ok = get_push_hub().push(PushMessage(
            title="⚠️ 配置漂移: 检测到非热重载的外部修改",
            body=(
                f"config hash: {old_hash} → {new_hash}\n"
                f"key 数: {diff.get('old_keys', '?')} → {diff.get('new_keys', '?')}\n"
                f"改动未经热重载链 ⇒ 需人工确认是否重启生效"
            ),
            level=Level.HIGH,
            category=Category.SYSTEM,
            dedup_key=f"config_drift:{old_hash}->{new_hash}",
        ))
        if not ok:
            logger.warning("[ConfigGov] 配置漂移告警 PushHub 未送达 (已落盘 push_failures.jsonl)")
    except Exception as e:
        # 回调异常不能影响 4h 巡检线程继续跑; 留 WARNING 便于判因
        logger.warning("[ConfigGov] 配置漂移告警推送失败: %s", e, exc_info=True)


def _run_new_scheduler(args):
    """启动 TradingScheduler: DI容器装配 + 多模型投票 + 自适应轮询 + Futu LV3"""
    import fcntl

    # DEB-20260903 #13: 双实例防护 — 原 /run 不可写时仅 print 警告继续启动, 防护静默失效
    #   (且 lock_fd 未定义 → cleanup_lock NameError). 现优先 /run, 失败回退数据目录锁;
    #   两者均失败则 fail-closed 退出 (双交易实例风险 > 不启动).
    # DEB-20260904 #140: 候选解析抽到 _lock_file_candidates(), 与 --stop/--status 共享
    lock_fd = None
    LOCK_FILE = None
    _lock_candidates = _lock_file_candidates()

    _lock_err = None
    for _cand in _lock_candidates:
        try:
            _fd = os.open(_cand, os.O_CREAT | os.O_RDWR)
            fcntl.flock(_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.truncate(_fd, 0)
            os.write(_fd, str(os.getpid()).encode())
            lock_fd, LOCK_FILE = _fd, _cand
            break
        except BlockingIOError:
            try:
                with open(_cand, 'r') as f:
                    existing_pid = f.read().strip()
                print(f"❌ FTSeek-US 已在运行 (PID: {existing_pid})")
            except Exception:
                print("❌ FTSeek-US 已在运行")
            sys.exit(1)
        except Exception as e:
            _lock_err = e
            continue

    if lock_fd is None:
        print(f"❌ 无法获取进程锁 ({_lock_err}) — 为防止双实例交易, 拒绝启动")
        sys.exit(1)

    import atexit
    def cleanup_lock():
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            os.unlink(LOCK_FILE)
        except Exception as _e:
            print(f"⚠️  进程锁清理失败 ({LOCK_FILE}): {_e}", file=sys.stderr)
    atexit.register(cleanup_lock)

    _startup_mode = _validate_startup_mode_or_exit()
    # LIVE-capable 判据: broker/db 任一 real, 或 ALLOW_REAL_TRADING=true
    # (含已授权的 LIVE 工程探针窗口 — 探针期同样不允许带着失效的退出策略校验跑).
    _live_capable = (
        _startup_mode["broker_route"] == "real"
        or _startup_mode["db_route"] == "live"
        or _startup_mode["allow_real_trading"]
    )

    # ── 启动前 OpenD 连通性检查 (P0 修复 2026-05-01) ──
    # 防止 OpenD 不可用时, Futu SDK _init_connect_sync 无限重试阻塞启动
    _opend_host = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
    # DEB-20260903 #13: 裸 int() 遇异常值直接崩溃 — 安全解析回退默认端口并告警
    try:
        _opend_port = int(os.environ.get("FUTU_OPEND_PORT", 11111) or 11111)
    except (TypeError, ValueError):
        logger.warning(
            f"[PreFlight] FUTU_OPEND_PORT={os.environ.get('FUTU_OPEND_PORT')!r} 解析失败, 回退默认 11111")
        _opend_port = 11111
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

    # (已移除 2026-07-25, 隐患扫描小项①) Task#36 (2026-06-16) trade_records schema 迁移:
    #   scripts/migrate_trade_records.py 已删 (schema 早已稳定, TradeStore 自建表),
    #   死引用每次启动报 WARNING 噪声。恢复: git revert 本块。

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

    # 【Phase US-1】美股盘前作战计划 — 2026-08-01 断线: battle plan 07-11 冻结,
    # playbook 现役链不消费, 启动加载+revalidate 仅产告警噪声 (07-31 已降级为告警)。
    # 墓碑: app/us_pipeline/battle_plan_manager.py 保留, 08-07 后随旧链批删。

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

        # Layer 1.5: Exit Policy 启动校验 (语义见 _exit_policy_startup_gate docstring)
        _exit_policy_startup_gate(_live_capable)

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

        # Layer 2: 漂移检测 (4 小时)
        # 2026-07-25 修复 (handoff 07-25 凌晨: ConfigGov 失联): Layer 2/3 启动挡在
        #   Layer 1.7 strict 审计之后 → 存量 BLOCKING (MIN_CONFIDENCE 漂移) raise 被外层
        #   "降级运行" 吞掉, 热重载/漂移检测根本没启动 (告警升级为功能损失)。
        #   调序: Layer 2/3 先启动, strict 审计后置 (告警/raise 语义不变)。回滚: 换回原顺序。
        drift_detector = ConfigDriftDetector(
            loader, interval_hours=4.0, on_drift_detected=_config_drift_alert,
        )
        drift_detector.start()
        logger.info("[ConfigGov] ✅ 漂移检测已启动 (interval=4h)")

        # Layer 3: 热重载 (60s 冷却) — 同步 ConfigContainer + 失效下游快照缓存
        def _on_config_reload():
            """热重载后同步 ConfigContainer._config_dict, 并失效读同一 yaml 的进程内快照.

            快照失效为何必需 (PlaybookConfig / FeatureFlags 两个"日志说重载了但链路仍读旧值"
            的消费者) 见 app/config_mgmt/hot_reloader.py::invalidate_config_snapshots。
            """
            from app.core.config_container import ConfigContainer
            ConfigContainer().reload()
            logger.info("[HotReloader] ✅ ConfigContainer 已同步")

            from app.config_mgmt.hot_reloader import invalidate_config_snapshots
            _invalidated = invalidate_config_snapshots()
            if _invalidated:
                logger.info("[HotReloader] ✅ 已失效进程内快照: %s", ", ".join(_invalidated))

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

        # Layer 1.7: SSOT 配置一致性审计 (YAML vs CFG 默认值, 严格模式)
        # (2026-07-25 后置到 Layer 2/3 之后, 见上方调序注释)
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

    except Exception as e:
        # DEB-20260903 #14: blocking 异常 (ConfigValidator RuntimeError /
        # ConfigAuditError) 不得被外层 except 吞掉 — 重新抛出保持阻断语义,
        # 否则配置治理的 blocking 机制形同虚设 (日志声称阻止但实际继续运行)
        if type(e).__name__ in ("RuntimeError", "ConfigAuditError"):
            raise
        logger.warning(f"[ConfigGov] 配置治理初始化失败 (降级运行): {e}")

    # 【新增 Phase US-2】初始化每日动态交易池 (v2 架构 2026-04-19)
    try:
        from app.utils.daily_active_pool import get_daily_active_pool
        
        pool = get_daily_active_pool()
        pool.initialize()

        # 1. 注入当前持仓 — 2026-07-16 修复: 从 open_positions (SSOT) 自愈式重建 POSITION 池。
        #   原实现读 data/trade_records.json (2026-03 死种子文件, 从不更新), 每次启动把 9 个
        #   3 月的幻影 SHORT 灌进池, 且无 env 隔离 (曾混入实盘/港股残留 US.HK.00700 等) →
        #   污染 inject 去重 + 扫描池, 压制真实交易机会。
        #   open_positions 已由 position_sync 持续对齐当前 SIM broker (owner=auto/sync=synced),
        #   干净且带方向/数量。自愈: 先删与真实持仓不符的陈旧 POSITION, 再按 SSOT 注入。
        try:
            import sqlite3 as _sq
            from app.core.position_sync import _get_db_path as _gdp
            from app.utils.daily_active_pool import SOURCE_POSITION as _SP
            _conn = _sq.connect(_gdp(), timeout=10)
            _rows = _conn.execute(
                "SELECT symbol, direction, size FROM open_positions WHERE symbol LIKE 'US.%'"
            ).fetchall()
            _conn.close()
            _live = {r[0] for r in _rows}
            for _e in list(pool.get_entries()):
                if _e.source == _SP and _e.code not in _live:
                    pool.remove_position(_e.code)
            if _rows:
                _pmap = {r[0]: {"direction": r[1], "quantity": r[2]} for r in _rows}
                pool.inject_positions(list(_live), position_map=_pmap)
                logger.info(f"💼 US 注入持仓 (源: open_positions SSOT): {len(_live)} 只 -> {sorted(_live)}")
            else:
                logger.info("💼 US 无当前持仓 (open_positions 空), 已清陈旧 POSITION")
        except Exception as _pe:
            logger.warning(f"💼 US 持仓注入失败 (源: open_positions): {_pe}")

        # 2. 注入盘前计划目标
        # 2026-08-03 (任务#6): 原 plan_path 未定义 → NameError, 每次启动报"初始化失败 降级运行".
        #   修复用 SSOT 路径 + 当日(ET)守卫 — 计划链已冻结 (07-13), 陈旧 plan 不得注入当日池.
        from app.core.paths import get_battle_plan_path
        plan_path = str(get_battle_plan_path())
        if os.path.exists(plan_path):
            with open(plan_path, 'r') as f:
                plan = json.loads(f.read())
            # 8/27 时钟统一: 月份 DST 猜测 → SSOT get_trade_date() (pytz/zoneinfo 真实切换)
            from app.utils.timezone import get_trade_date as _get_td_bp
            _today_et_bp = _get_td_bp()
            if plan and plan.get('targets') and plan.get('date') == _today_et_bp:
                pool.inject_plan_targets(plan)
                logger.info(f"📝 US 注入盘前计划: {len(plan['targets'])} 只")
            elif plan and plan.get('targets'):
                logger.info(f"📝 US 盘前计划非当日 (date={plan.get('date')} ≠ {_today_et_bp}), 跳过注入 (计划链已冻结)")

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
        # P0 (2026-10-18 推送治理批): 30→60min 可配置。原 30min × 6.5h = 13 条/日,
        #   实测 10/16 全量推送 34 条中心跳占 13 条 (38%)。心跳是状态播报不是告警,
        #   告警/异常仍有 error_alert + 通道熔断兜底。回滚: 配置改回 30。
        _hb_interval_min = 60
        try:
            from app.core.config_container import get_config as _hb_gc
            _hb_interval_min = int(_hb_gc().get_dotted(
                "push.intraday_heartbeat_interval_min", 60))
        except Exception as _hb_e:
            logger.warning(f"⚠️ 心跳间隔配置读取失败, 用默认 60min: {_hb_e}")
        _heartbeat = HeartbeatLoop(interval_min=_hb_interval_min, scheduler=scheduler)
        _heartbeat.start()
        logger.info(
            f"💓 盘中心跳后台线程已启动 (interval={_hb_interval_min}min, 仅盘中触发)")
    except Exception as e:
        logger.warning(f"⚠️ 盘中心跳启动失败 (不影响主流程): {e}")

    # 【新增 Phase 3】启动突发拦截器后台监控
    # 2026-07-31 议题④ E1 (DEB-cleanup-remaining, 用户裁"整体执行"): 假哨兵线程退役 —
    #   position_store 恒空 → 拦截器 6/12 起零触发 (结构性失效); 其仅存活价值
    #   auto_recovery 驱动已迁至 position_sync 30s 心跳 (position_sync.py _sync_loop),
    #   kill switch 检查已直挂 playbook auto_execute 入口 (批2②)。
    #   check_kill_switch/activate/deactivate 三个纯函数保留 (公共 API 多处 import)。
    #   回滚: 解注下方块。
    # try:
    #     import threading
    #     from app.us_pipeline.emergency_interceptor import USEmergencyInterceptor
    #     interceptor = USEmergencyInterceptor(scheduler, execution_engine)
    #     async def run_interceptor():
    #         await interceptor.start_monitoring()
    #     interceptor_thread = threading.Thread(target=asyncio.run, args=(run_interceptor(),), daemon=True, name="EmergencyInterceptor")
    #     interceptor_thread.start()
    #     logger.info("🛡️ 美股突发拦截器后台线程已启动")
    # except Exception as e:
    #     logger.warning(f"⚠️ 启动突发拦截器失败: {e}")
    logger.info("🛡️ 突发拦截器线程已退役 (E1: auto_recovery→position_sync 心跳, kill_switch→playbook 入口直挂)")

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
        if _broker is not None:
            # 审计 (2026-08-24): 判定源与 order_executor.py:263 对齐 — 直接取
            # broker_adapter.trade_env (8/16 SoT 迁移后为权威环境), 不再读
            # 已废弃的 TRADE_ENV。原写法切实盘且 TRADE_ENV 未同步时会查错环境。
            _trd_env = _TrdEnv.REAL if getattr(_broker, "trade_env", 1) != 1 else _TrdEnv.SIMULATE
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


def _stop_service():
    """DEB-20260903 #15: --stop 实现 — 发送 SIGTERM 到运行中的主进程"""
    import signal as _sig
    # DEB-20260904 #140: 原硬编码 /run 锁, 启动回退数据目录锁时误报未运行 — 走共享解析
    lock_file = _resolve_lock_file()
    try:
        with open(lock_file, 'r') as f:
            pid = int(f.read().strip())
        os.kill(pid, _sig.SIGTERM)
        print(f"✅ 已发送 SIGTERM 到 FTSeek-US (PID: {pid})")
    except FileNotFoundError:
        print("❌ FTSeek-US 未在运行 (无锁文件)")
        sys.exit(1)
    except ProcessLookupError:
        print(f"❌ 进程已不存在 (PID: {pid}), 清理锁文件")
        try:
            os.unlink(lock_file)
        except Exception:
            pass
        sys.exit(1)
    except Exception as e:
        print(f"❌ 停止失败: {e}")
        sys.exit(1)


def _show_status():
    """DEB-20260903 #15: --status 实现 — 检查主进程是否存活"""
    # DEB-20260904 #140: 原硬编码 /run 锁, 启动回退数据目录锁时误报未运行 — 走共享解析
    lock_file = _resolve_lock_file()
    try:
        with open(lock_file, 'r') as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)  # 探测进程是否存在
        print(f"✅ FTSeek-US 运行中 (PID: {pid})")
    except FileNotFoundError:
        print("❌ FTSeek-US 未运行")
        sys.exit(1)
    except ProcessLookupError:
        print(f"❌ FTSeek-US 未运行 (锁文件残留 PID={pid})")
        sys.exit(1)
    except PermissionError:
        print(f"✅ FTSeek-US 运行中 (PID: {pid}, 无权限探测)")
    except Exception as e:
        print(f"❌ 状态检查异常: {e}")
        sys.exit(1)


def _test_connections():
    """DEB-20260903 #15: --test 实现 — 仅做连通性检查, 不启动交易调度器"""
    import socket as _sock
    ok = True

    # 1. OpenD 连通性
    host = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
    # DEB-20260904 #141: 安全解析端口 — 与启动 PreFlight (DEB-20260903 #13) 同口径,
    # 裸 int() 遇异常值直接抛 ValueError 中断 --test
    try:
        port = int(os.environ.get("FUTU_OPEND_PORT", 11111) or 11111)
    except (TypeError, ValueError):
        print(f"⚠️  FUTU_OPEND_PORT={os.environ.get('FUTU_OPEND_PORT')!r} 解析失败, 回退默认 11111")
        port = 11111
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect((host, port))
        print(f"✅ OpenD 连通: {host}:{port}")
    except Exception as e:
        print(f"❌ OpenD 不可达: {host}:{port} — {e}")
        ok = False
    finally:
        s.close()

    # 2. 主 DB 可读
    try:
        from app.core.db_utils import get_main_db
        import sqlite3 as _sq
        db_path = str(get_main_db())
        conn = _sq.connect(db_path, timeout=3)
        conn.execute("SELECT 1")
        conn.close()
        print(f"✅ 主 DB 可读: {db_path}")
    except Exception as e:
        print(f"❌ 主 DB 异常: {e}")
        ok = False

    # 3. 配置文件可读
    try:
        yaml_path = os.path.join(PROJECT_DIR, "config", "unified_config.yaml")
        import yaml
        with open(yaml_path) as f:
            yaml.safe_load(f)
        print(f"✅ 配置文件可读: {yaml_path}")
    except Exception as e:
        print(f"❌ 配置文件异常: {e}")
        ok = False

    # 4. 告警通道
    has_ding = bool(os.getenv("DINGTALK_WEBHOOK_URL_US", "").strip())
    has_fei = bool(os.getenv("FEISHU_WEBHOOK_URL_US", "").strip()) or bool(os.getenv("FEISHU_APP_ID", "").strip())
    if has_ding or has_fei:
        ch = []
        if has_ding: ch.append("DingTalk")
        if has_fei: ch.append("Feishu")
        print(f"✅ 告警通道: {', '.join(ch)}")
    else:
        print("⚠️  未配置告警通道 (DingTalk/Feishu)")

    if ok:
        print("\n✅ 所有连通性检查通过")
    else:
        print("\n❌ 存在连通性问题")
        sys.exit(1)


def main():
    """主函数"""
    import argparse

    parser = argparse.ArgumentParser(description="FTSeek-US 美股AI投研系统")
    parser.add_argument("--start", action="store_true", help="启动服务")
    parser.add_argument("--stop", action="store_true", help="停止服务")
    parser.add_argument("--status", action="store_true", help="查看状态")
    parser.add_argument("--test", action="store_true", help="测试连接")

    args = parser.parse_args()

    # DEB-20260903 #15: 按参数分派, 不再无条件启动调度器
    if args.stop:
        _stop_service()
    elif args.status:
        _show_status()
    elif args.test:
        _test_connections()
    else:
        _run_new_scheduler(args)


if __name__ == "__main__":
    main()
