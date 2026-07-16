"""
持仓监控管线 — FTSeek-US 持仓监控、止损止盈、对账

从 scheduler_tasks.py 拆分而来。本模块包含完整的持仓监控循环、
期权管理、资本安全水位追踪、Broker 对账。
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import datetime
from typing import Dict, Any, List, Optional

from app.core.models import (
    Position, Direction, MarketRegime,
    Order, OrderType, ExitReason, TradeEvent, AlertLevel,
)
from app.utils.timezone import _et_trade_date, now_et
from app.risk.position_exit_monitor import get_exit_monitor

logger = logging.getLogger(__name__)


# ============================================================
# Phase 4 (2026-05-31): ExitRuleDispatcher 正式路由集成
#   - 由 unified_config.yaml::exit_rules_controller 控制
#   - 触发后通过 _execute_close 走与 legacy 完全相同的下单/DB 写入路径
#   - 异常或未触发的仓位仍由 legacy PositionExitMonitor.scan_positions 处理
# ============================================================

# Dispatcher 规则名 -> legacy ExitReason 映射 (供 _execute_close 内 reason_map 使用)
# P0 修复 (2026-06-03): day_trade 拆分为 TP/timeout 两种语义, 由 _build_exit_reason_dict 动态判断
_DISPATCHER_REASON_MAP: Dict[str, str] = {
    "flash_exit": "FLASH_EXIT",
    "pre_close": "PRE_CLOSE_EXIT",
    "circuit_breaker": "HARD_STOP",
    "earnings_guard": "HARD_STOP",
    "trailing_stop": "CHANDELIER",
    "day_trade": "DAY_TRADE",  # 占位, 实际由 _build_exit_reason_dict 细化为 DAY_TRADE_TP / DAY_TRADE_TIMEOUT
    "time_decay": "TIME_STOP",
    "scale_out": "SCALE_OUT",
}

# ── P0 (2026-06-02): 退出屏障 — 防止闪退后立即再入场 ──
# 退出执行成功后，同一标的在 barrier_seconds 内禁止开仓
# 解决: flash_exit → 立即 signal_pipeline 再入场 → 再次 flash_exit 的死循环
_EXIT_BARRIER: Dict[str, float] = {}  # symbol → barrier_until_timestamp
_EXIT_BARRIER_SECONDS = 1800  # 30 分钟冷却 (2026-06-25: 600→1800, 进一步抑制闪退死循环)
_EXIT_BARRIER_FILE = "/tmp/ftseek_exit_barrier.json"
# H3 (2026-06-03): 线程安全锁 — 保护 dict 读写 + 文件持久化原子性
_EXIT_BARRIER_LOCK = threading.Lock()

# 从磁盘恢复退出屏障 (防止重启丢失)
try:
    if os.path.exists(_EXIT_BARRIER_FILE):
        import json as _json
        with open(_EXIT_BARRIER_FILE, 'r') as _f:
            _barrier_data = _json.load(_f)
            _now = time.time()
            # 只恢复尚未过期的屏障
            for _sym, _until in _barrier_data.items():
                if _until > _now:
                    _EXIT_BARRIER[_sym] = _until
        if _EXIT_BARRIER:
            logger.info(f"[退出屏障·恢复] 从磁盘恢复 {len(_EXIT_BARRIER)} 个标的屏障")
except Exception as _e:
    logger.debug(f"[退出屏障·恢复] 失败 (不影响启动): {_e}")


def _save_exit_barrier():
    """H3: 持久化退出屏障到磁盘 (线程安全, 原子写入)"""
    try:
        import json as _json
        _now = time.time()
        with _EXIT_BARRIER_LOCK:
            # H4 关联: 顺带清理过期条目
            _expired = [s for s, u in _EXIT_BARRIER.items() if u <= _now]
            for s in _expired:
                del _EXIT_BARRIER[s]
            # 原子写入: 先写临时文件再 rename (防损坏)
            _tmp_file = _EXIT_BARRIER_FILE + ".tmp"
            with open(_tmp_file, 'w') as _f:
                _json.dump(dict(_EXIT_BARRIER), _f)
            os.replace(_tmp_file, _EXIT_BARRIER_FILE)
    except Exception as _e:
        logger.debug(f"[退出屏障·保存] 失败: {_e}")


def _check_exit_barrier(symbol: str) -> bool:
    """H3: 检查标的是否在退出屏障中 (线程安全)

    Returns:
        True = 在屏障中 (禁止入场), False = 不在 (允许入场)
    """
    with _EXIT_BARRIER_LOCK:
        barrier_until = _EXIT_BARRIER.get(symbol, 0)
    return time.time() < barrier_until


def _set_exit_barrier(symbol: str):
    """H3: 设置退出屏障 (线程安全)"""
    with _EXIT_BARRIER_LOCK:
        _EXIT_BARRIER[symbol] = time.time() + _EXIT_BARRIER_SECONDS
    _save_exit_barrier()


def _get_exit_controller_config() -> Dict[str, Any]:
    """读取 unified_config.yaml::exit_rules_controller (best-effort)"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        if cfg is None:
            return {}
        section = cfg.get_section("exit_rules_controller", {}) or {}
        if not isinstance(section, dict):
            return {}
        return section
    except Exception as e:
        logger.debug(f"[ExitDispatcher] 读取 exit_rules_controller 失败: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────
# RFC-057 (2026-06-01): grace period 入场保护
# 数据驱动 (n=60, 90天): <30min 持仓 31.7% wr -$27,437 (占总亏损 188%)
# 30min-1h: 25% wr +$14,970 — 放过噪声窗口反而盈利
# 配置 SSOT: app/risk/exit_policy.yaml::global_hold_protection
# ─────────────────────────────────────────────────────────────────────────

_GRACE_CFG_CACHE: Dict[str, Any] = {}
_GRACE_CFG_CACHE_TS: float = 0.0
_GRACE_CFG_TTL: float = 60.0  # 配置缓存 60s, 避免每次 SQL 调用


def _get_grace_protection_config() -> Dict[str, Any]:
    """读取 exit_policy.yaml::global_hold_protection (60s 缓存)"""
    global _GRACE_CFG_CACHE, _GRACE_CFG_CACHE_TS
    now = time.time()
    if now - _GRACE_CFG_CACHE_TS < _GRACE_CFG_TTL and _GRACE_CFG_CACHE:
        return _GRACE_CFG_CACHE
    try:
        import yaml as _yaml
        from app.core.paths import get_exit_policy_path
        path = get_exit_policy_path()
        if not path or not path.exists():
            return {}
        with open(path, 'r') as f:
            policy = _yaml.safe_load(f)
        cfg = (policy or {}).get("global_hold_protection", {}) or {}
        _GRACE_CFG_CACHE = cfg if isinstance(cfg, dict) else {}
        _GRACE_CFG_CACHE_TS = now
        return _GRACE_CFG_CACHE
    except Exception as e:
        logger.debug(f"[grace_period] 配置读取失败 (规则不生效): {e}")
        return {}


def _check_grace_period_block(position, cur_price: float, rule_name: str) -> Optional[str]:
    """RFC-057: 判断是否在 grace period 且当前 rule 应被屏蔽

    Returns:
        屏蔽原因字符串 (字符串非空 = 屏蔽) 或 None (放行)
    """
    cfg = _get_grace_protection_config()
    if not cfg.get("enabled", False):
        return None

    min_minutes = float(cfg.get("min_hold_minutes_after_entry", 30))
    if min_minutes <= 0:
        return None

    # 计算入场时长
    entry_time = getattr(position, "entry_time", None)
    if entry_time is None:
        return None  # 没有入场时间, 放行
    try:
        if hasattr(entry_time, "timestamp"):
            entry_ts = entry_time.timestamp()
        else:
            entry_ts = float(entry_time)
        elapsed_min = (time.time() - entry_ts) / 60.0
    except Exception:
        return None

    if elapsed_min >= min_minutes:
        return None  # 已过 grace period

    # max_grace_loss_pct 兜底: 浮亏 >= 此值立即解除保护
    try:
        max_loss = float(cfg.get("max_grace_loss_pct", 0.03))
        entry_price = float(getattr(position, "avg_entry_price", 0) or 0)
        if entry_price > 0 and cur_price > 0:
            direction = getattr(position, "direction", None)
            dir_str = direction.value if hasattr(direction, "value") else str(direction)
            if dir_str == "LONG":
                pnl_pct = (cur_price - entry_price) / entry_price
            else:
                pnl_pct = (entry_price - cur_price) / entry_price
            if pnl_pct <= -max_loss:
                # 浮亏达到底线, 解除保护放行所有规则
                logger.warning(
                    f"[grace_period·硬底解除] {getattr(position,'symbol','?')} "
                    f"PnL={pnl_pct*100:.2f}% <= -{max_loss*100:.1f}% 解除保护"
                )
                return None
    except Exception as e:
        logger.debug(f"[grace_period] 检查异常 (放行): {e}")

    # 规则白名单检查
    allowed = cfg.get("allowed_during_grace", []) or []
    if rule_name in allowed:
        return None  # 白名单内规则放行

    return f"grace {elapsed_min:.1f}min < {min_minutes:.0f}min, rule={rule_name} 不在白名单"


def _build_exit_dispatcher(scheduler):
    """懒构建 ExitRuleDispatcher 单例并缓存到 scheduler"""
    cached = getattr(scheduler, "_exit_dispatcher", None)
    if cached is not None:
        return cached
    try:
        from app.risk.exit_rule_dispatcher import get_exit_rule_dispatcher
        from app.risk.exit_policy_guardian import get_guardian
        guardian = get_guardian()  # 单例: 内部已 load()
        policy = getattr(guardian, "policy", None) or {}
        dispatcher = get_exit_rule_dispatcher(policy if isinstance(policy, dict) else {})
        scheduler._exit_dispatcher = dispatcher
        if dispatcher is not None:
            n_rules = len(getattr(dispatcher, "exit_rules", {}) or {})
            logger.info(
                f"[ExitDispatcher] ExitRuleDispatcher 已初始化 (主路径, 规则数={n_rules})"
            )
        return dispatcher
    except Exception as e:
        logger.warning(f"[ExitDispatcher] 初始化失败: {e}")
        return None


def _build_dispatcher_position_dict(position) -> Dict[str, Any]:
    """将 Position 对象转为 dispatcher.evaluate() 期望的 dict"""
    direction_str = "LONG"
    try:
        d = getattr(position, "direction", None)
        if d is not None:
            direction_str = d.value if hasattr(d, "value") else str(d)
    except Exception:
        pass
    holding_hours = 0.0
    try:
        if getattr(position, "entry_time", None):
            from app.risk.position_exit_monitor import get_market_hours_us
            holding_hours = get_market_hours_us(position.entry_time, now_et())
    except Exception:
        pass
    # 注入 entry_time_ts (供 ExitRuleDispatcher._eval_flash_exit 使用)
    entry_time_ts = 0.0
    try:
        if getattr(position, "entry_time", None):
            if hasattr(position.entry_time, "timestamp"):
                entry_time_ts = position.entry_time.timestamp()
            else:
                entry_time_ts = float(position.entry_time)
    except Exception:
        pass
    return {
        "symbol": getattr(position, "symbol", ""),
        "position_id": getattr(position, "position_id", "") or getattr(position, "symbol", ""),
        "entry_price": float(getattr(position, "avg_entry_price", 0) or 0),
        "direction": direction_str,
        "holding_hours": holding_hours,
        "entry_time_ts": entry_time_ts,
        "owner_strategy": (
            getattr(position, "owner_strategy", "")
            or getattr(position, "strategy_id", "")
            or "main"
        ),
    }


def _build_dispatcher_market_data(scheduler, position, current_price: float) -> Dict[str, Any]:
    """为 dispatcher.evaluate() 构建 market_data 字典 (ATR 来自 ATRProvider，Task #4)"""
    md: Dict[str, Any] = {
        "current_price": float(current_price),
        "now_ts": time.time(),  # 供 flash_exit min_holding_seconds 使用
    }

    # ADR-004 (2026-06-04): 传递 regime 给 dispatcher — 使 chandelier_trail multiplier_by_regime 生效
    # 根因: dispatcher._eval_trailing_stop 用 market_data.get("regime") 查 regime,
    #        但 _build_market_data 从未传递 regime → 永远 fallback 到 "normal" (2.5x)
    #        → RANGE 市的 99.0x 配置永远不匹配 → 追踪止损在震荡照常触发
    try:
        _regime = getattr(position, "market_regime", None)
        if _regime is not None:
            md["regime"] = _regime.value if hasattr(_regime, "value") else str(_regime)
        else:
            md["regime"] = "UNKNOWN"
    except Exception:
        md["regime"] = "UNKNOWN"

    # ATR 单一真相源
    try:
        from app.core.di_container import get_di_container
        atr_provider = get_di_container().atr_provider()
        if atr_provider is not None:
            atr_val = atr_provider.get_atr(position.symbol)
            if atr_val is not None and float(atr_val) > 0:
                atr_val = float(atr_val)
                # Apply session-aware blending (matching legacy path behavior)
                try:
                    from app.risk.position_exit_monitor import get_market_hours_us
                    if hasattr(position, 'entry_time') and position.entry_time:
                        hours = get_market_hours_us(position.entry_time, now_et())
                        # Session ratio: 0→1 as market progresses from open to close
                        session_ratio = min(1.0, hours / 6.5)  # 6.5h trading day
                        # Apply cosine smoothing (matching progressive_trailing.session_blend_window_minutes)
                        blend_minutes = 90.0  # from exit_policy.yaml progressive_trailing
                        blend_hours = blend_minutes / 60.0
                        if hours < blend_hours:
                            # Smooth transition from entry session ratio to current
                            t = hours / blend_hours
                            smooth = (1 - math.cos(t * math.pi)) / 2
                            session_ratio = smooth * session_ratio + (1 - smooth) * 1.0
                        effective_atr = atr_val * max(0.35, session_ratio)  # floor at 0.35
                        # P0 (2026-06-04): ATR 百分比限幅 — 防止 1-min ATR 数据异常
                        # 根因: 1-min K线ATR在盘前/低流动性时可能异常小(BABA=$0.02)
                        #       或高波股异常大(AMD ATR%=2.69% → 4xATR止损=10.77%)
                        _min_atr_pct = 0.005   # 0.5% floor: ATR不能低于价格的0.5%
                        _max_atr_pct = 0.025   # 2.5% ceiling: ATR不能高于价格的2.5%
                        if current_price > 0:
                            _atr_pct = effective_atr / current_price
                            if _atr_pct < _min_atr_pct:
                                effective_atr = current_price * _min_atr_pct
                                logger.debug(f"[ATR限幅] {position.symbol} ATR%={_atr_pct:.4f}<{_min_atr_pct} → floor=${effective_atr:.4f}")
                            elif _atr_pct > _max_atr_pct:
                                effective_atr = current_price * _max_atr_pct
                                logger.debug(f"[ATR限幅] {position.symbol} ATR%={_atr_pct:.4f}>{_max_atr_pct} → ceiling=${effective_atr:.4f}")
                        md["atr"] = float(effective_atr)
                        md["atr_raw"] = float(atr_val)
                    else:
                        md["atr"] = float(atr_val)
                except Exception:
                    md["atr"] = float(atr_val)
    except Exception as e:
        logger.debug(f"[ExitDispatcher] {position.symbol} ATR 获取失败: {e}")

    # minutes_to_close (供 pre_close 自适应规则使用)
    try:
        now = now_et()
        close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
        delta_min = (close_dt - now).total_seconds() / 60.0
        md["minutes_to_close"] = delta_min
    except Exception:
        pass

    # daily_loss_pct (供 circuit_breaker 使用，best-effort)
    try:
        cap_safety = getattr(scheduler, "capital_safety", None)
        if cap_safety is not None and hasattr(cap_safety, "get_today_drawdown_pct"):
            dl = cap_safety.get_today_drawdown_pct()
            if dl is not None:
                md["daily_loss_pct"] = float(dl)
    except Exception:
        pass

    return md


def _build_exit_reason_dict(position, decision, current_price: float) -> Dict[str, Any]:
    """将 ExitDecision 转为 _execute_close 期望的 reason_dict"""
    reason_key = _DISPATCHER_REASON_MAP.get(decision.rule_name, "ATR_STOP")

    # P0 修复 (2026-06-03): day_trade 区分止盈/超时两种语义
    # ExitDecision.reason 字段包含 "Day trade TP: ..." 或 "Day trade timeout: ..."
    if decision.rule_name == "day_trade":
        _dt_reason = (decision.reason or "").lower()
        if "tp" in _dt_reason or "profit" in _dt_reason or "take_profit" in _dt_reason:
            reason_key = "DAY_TRADE_TP"
        else:
            reason_key = "DAY_TRADE_TIMEOUT"

    reason_dict: Dict[str, Any] = {
        "reason": reason_key,
        "exit_price": decision.exit_price or current_price,
        "rule_name": decision.rule_name,
        "dispatcher_reason": decision.reason,
    }
    md = decision.metadata or {}
    size_int = int(getattr(position, "size", 0) or 0)

    # scale_out: 部分平仓 (close_pct: 25 表示平 25%)
    if decision.rule_name == "scale_out":
        try:
            close_pct = float(md.get("close_pct") or 0)
            if 0 < close_pct < 100 and size_int > 0:
                reason_dict["close_qty"] = max(1, int(size_int * close_pct / 100.0))
        except Exception as e:
            logger.warning(f"[退出] scale_out close_qty 计算失败 {position.symbol}: {e}")
    # pre_close: 自适应分级 close_ratio (0.0~1.0)
    elif decision.rule_name == "pre_close":
        try:
            cr = md.get("close_ratio")
            if cr is not None:
                cr_f = float(cr)
                if 0 < cr_f < 1.0 and size_int > 0:
                    reason_dict["close_qty"] = max(1, int(size_int * cr_f))
        except Exception as e:
            logger.warning(f"[退出] pre_close close_qty 计算失败 {position.symbol}: {e}")
    # For pre_close with close_ratio < 1.0, use PARTIAL reason
    if decision.rule_name == "pre_close":
        md2 = decision.metadata or {}
        cr = float(md2.get("close_ratio", 1.0) or 1.0)
        if cr < 1.0:
            reason_dict["reason"] = "PRE_CLOSE_EXIT_PARTIAL"
    return reason_dict


def _notify_risk_blocked(scheduler, symbol: str, reason: str):
    """风控拦截通知"""
    logger.debug(f"[风控] {symbol} 被拦截：{reason}")


def _partial_close_position(scheduler, position: Position, current_price: float, close_qty: int, exit_reason: ExitReason):
    """部分平仓 — 分批止盈专用，减少仓位但不全平"""
    from app.pipeline.order_executor import _close_position

    # ── P1 (2026-06-16): 手动持仓防护 — 与 _execute_close 的 DB 检查一致 ──
    # 作为安全网: 即使被绕过 _execute_close 直接调用, 也能拦截
    try:
        _pc_sym = getattr(position, "symbol", "")
        if _pc_sym:
            from app.core.db_utils import get_main_db
            import sqlite3 as _pc_sql
            _pc_conn = _pc_sql.connect(str(get_main_db()), timeout=3)
            _pc_owner = _pc_conn.execute(
                "SELECT owner FROM open_positions WHERE symbol=?", (_pc_sym,)
            ).fetchone()
            _pc_conn.close()
            if _pc_owner and (_pc_owner[0] or "").lower() == "manual":
                logger.warning(
                    f"[分批止盈·拒绝] {position.symbol} owner=manual, 跳过自动分批止盈"
                )
                return
    except Exception:
        pass  # DB 不可用时不阻断 (fail open for partial — less risky than full close)

    # ── P0 (2026-06-12): Scale-out 重入防护 ──
    # 根因: SNDK 灾难 — 分批止盈成功后 position_sync 覆盖 size，
    #        下一 tick 又触发，连续 BUY 导致 SHORT→LONG 方向翻转
    # 修法: 成功 scale-out 后 10 分钟内不再触发同一标的
    _meta = getattr(position, "metadata", {}) or {}
    _so_until = _meta.get("_scale_out_until", 0)
    if time.time() < _so_until:
        _remain = int(_so_until - time.time())
        logger.debug(
            f"[分批止盈·冷却] {position.symbol} scale-out 冷却中 "
            f"(剩余 {_remain}s)，跳过"
        )
        return
    # 设置冷却: 10 分钟 (足够 position_sync 完成结算)
    _meta["_scale_out_until"] = time.time() + 600
    position.metadata = _meta

    # ── P0 (2026-06-12): Broker 仓位预检 ──
    # 根因: SNDK 灾难 — 内存缓存 SHORT 10, 但 broker 已经 FLAT 或 LONG,
    #        系统发 BUY 5 "平仓" 变成了新建 LONG 仓位
    # 修法: 下单前直接查 broker 实际仓位, 方向/数量不符则中止
    try:
        _adapter = getattr(getattr(scheduler, 'execution_engine', None), 'broker_adapter', None)
        if _adapter and hasattr(_adapter, 'get_positions'):
            _broker_pos = _adapter.get_positions() or []
            _bp = None
            for _b in _broker_pos:
                _bsym = _b.get("symbol") if isinstance(_b, dict) else getattr(_b, "symbol", None)
                if _bsym == position.symbol:
                    _bp = _b
                    break
            if _bp is None:
                logger.warning(
                    f"🚫 [分批止盈·broker预检] {position.symbol} broker 端无持仓, "
                    f"中止 scale-out (可能已被外部平仓)"
                )
                return
            _bqty = float(_b.get("qty", 0) if isinstance(_b, dict) else getattr(_b, "qty", 0))
            _b_side = _bp.get("position_side", "") if isinstance(_bp, dict) else getattr(_bp, "position_side", "")
            _broker_is_short = _bqty < 0 or str(_b_side).upper() == "SHORT"
            _mem_is_short = position.direction == Direction.SHORT
            if _broker_is_short != _mem_is_short:
                logger.critical(
                    f"🔴 [分批止盈·方向冲突] {position.symbol} "
                    f"内存={position.direction.value} vs "
                    f"Broker={'SHORT' if _broker_is_short else 'LONG'} — "
                    f"中止 scale-out, 等待 position_sync 修正"
                )
                return
            _abs_bqty = abs(_bqty)
            if _abs_bqty < close_qty:
                logger.warning(
                    f"🚫 [分批止盈·数量不足] {position.symbol} "
                    f"broker 仅有 {_abs_bqty} 股, 需要平 {close_qty} 股 — 中止"
                )
                return
    except Exception as _precheck_err:
        # 预检失败不阻断 — 宁可多发一单也不能因为网络问题卡住
        logger.debug(f"[分批止盈·预检异常] {position.symbol}: {_precheck_err}")

    if close_qty <= 0 or close_qty >= position.size:
        _close_position(scheduler, position, current_price, exit_reason)
        return

    # ── P0 (2026-06-12): 设置 _close_in_progress 防止并发全平 ──
    # 根因: _partial_close_position 不设锁 → 同时触发的 hard_stop/EMERGENCY
    #        可以发起全平 → 两笔卖单同时到达 broker → 反向仓位
    _pc_meta = getattr(position, "metadata", {}) or {}
    _pc_meta["_close_in_progress"] = True
    _pc_meta["_close_in_progress_ts"] = time.time()
    position.metadata = _pc_meta

    if position.direction == Direction.LONG:
        pnl = (current_price - position.avg_entry_price) * close_qty
        pnl_pct = (current_price - position.avg_entry_price) / position.avg_entry_price if position.avg_entry_price > 0 else 0
    else:
        pnl = (position.avg_entry_price - current_price) * close_qty
        pnl_pct = (position.avg_entry_price - current_price) / position.avg_entry_price if position.avg_entry_price > 0 else 0

    logger.info(
        f"[分批止盈] {position.symbol} Tier触发 | "
        f"平仓 {close_qty}/{int(position.size)} 股 | "
        f"预估PnL=${pnl:.2f} ({pnl_pct:.2%}) | "
        f"剩余 {int(position.size - close_qty)} 股"
    )

    close_order = Order(
        client_order_id=f"{position.symbol}_scaleout_{int(time.time())}",
        signal_id=position.signal_id,
        symbol=position.symbol,
        direction=Direction.SHORT if position.direction == Direction.LONG else Direction.LONG,
        order_type=OrderType.MARKET,
        quantity=close_qty,
        price=current_price,
    )
    if not hasattr(close_order, 'metadata') or close_order.metadata is None:
        close_order.metadata = {}
    close_order.metadata['is_exit'] = True

    try:
        if scheduler.execution_engine:
            result_order = scheduler.execution_engine.submit_order(close_order)
            # 2026-06-06: 检查 broker 结果 — 拒单时不扣 position.size
            from app.core.models import OrderState as _OS
            if result_order and getattr(result_order, 'state', None) == _OS.REJECTED:
                logger.error(
                    f"🚫 [分批止盈·拒单] {position.symbol} broker拒绝: "
                    f"{getattr(result_order, 'failure_reason', 'unknown')} — 不修改仓位"
                )
                # 清理 _close_in_progress
                _pc_meta2 = getattr(position, "metadata", {}) or {}
                _pc_meta2.pop("_close_in_progress", None)
                _pc_meta2.pop("_close_in_progress_ts", None)
                position.metadata = _pc_meta2
                return
            # H5 (2026-06-03): position.size 修改需持有 PositionStore 锁, 防止 scan 线程读取中间值
            with scheduler.position_store._lock:
                position.size -= close_qty
                # 2026-06-06: 标记已部分减仓 (替代 trail_state 追踪, DB持久化)
                position.is_partially_reduced = True
            logger.info(
                f"[分批止盈·完成] {position.symbol} 仓位更新: "
                f"{int(position.size + close_qty)} → {int(position.size)} 股 | is_partially_reduced=True"
            )
            try:
                if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
                    scheduler.trade_store.upsert_open_position(
                        position, source="scheduler", action="partial_close",
                        is_partially_reduced=True,
                    )
            except Exception as db_err:
                logger.debug(f"[分批止盈] DB 同步失败: {db_err}")
            # P0 修复 (2026-05-11): 亏损的部分平仓也需要更新策略熔断计数器
            if pnl < 0 and scheduler.risk_engine and hasattr(scheduler.risk_engine, "strategy_breaker"):
                try:
                    _strat_id = getattr(position, "strategy_id", "") or getattr(position, "owner_strategy", "") or "main_pipeline"
                    scheduler.risk_engine.strategy_breaker.record_loss(_strat_id)  # P0 (2026-06-05): 真实策略名
                except Exception as e:
                    logger.debug(f"[策略熔断] record_loss 失败 {_strat_id}: {e}")
    except Exception as e:
        logger.error(f"[分批止盈·失败] {position.symbol}: {e}")

    # P0 (2026-06-12): 统一清理 _close_in_progress (无论成功/失败/异常)
    # _scale_out_until 仍然有效 (10min冷却), _close_in_progress 仅保护执行期间
    _pc_meta_final = getattr(position, "metadata", {}) or {}
    _pc_meta_final.pop("_close_in_progress", None)
    _pc_meta_final.pop("_close_in_progress_ts", None)
    position.metadata = _pc_meta_final



def _load_price_fail_closed_set() -> set:
    """加载 PRICE_FAIL 已关闭的期权代码集合，防止同步循环"""
    try:
        import json as _json
        _pf_p = os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'price_fail_closed.json'))
        if os.path.exists(_pf_p):
            return set(_json.load(open(_pf_p)))
    except Exception:
        pass
    return set()


def restore_positions_from_db(scheduler):
    """服务重启后从 DB 快照恢复未平仓持仓到内存"""
    try:
        # P0 修复 (2026-05-21): 加载 PRICE_FAIL 已关闭集合，跳过这些期权
        _pf_closed = _load_price_fail_closed_set()
        if _pf_closed:
            logger.info(f"[持仓恢复] PRICE_FAIL 已关闭集合: {_pf_closed}")

        positions = scheduler.trade_store.load_open_positions()
        if not positions:
            logger.info("[持仓恢复] DB 无未平仓记录")
            return 0

        # 2026-06-01: 修复 NameError — 拉一份 broker 实盘持仓做对账, 失败时 fallback 空 dict
        # is_anomalous 自愈逻辑 (line 568-) 需要 broker_positions_map 验证 DB qty 与 broker qty 一致
        broker_positions_map: Dict[str, Dict[str, Any]] = {}
        try:
            adapter = getattr(getattr(scheduler, "execution_engine", None), "broker_adapter", None)
            if adapter and hasattr(adapter, "get_positions"):
                _broker_positions = adapter.get_positions() or []
                for _bp in _broker_positions:
                    _sym = _bp.get("symbol") if isinstance(_bp, dict) else getattr(_bp, "symbol", None)
                    _qty = _bp.get("qty") if isinstance(_bp, dict) else getattr(_bp, "qty", None)
                    if _sym and _qty is not None:
                        broker_positions_map[_sym] = {"qty": float(_qty)}
                logger.info(f"[持仓恢复] Broker 持仓对账: {len(broker_positions_map)} 个")
        except Exception as _bpe:
            logger.error(f"[持仓恢复] Broker 持仓拉取失败, 放弃 DB 恢复 (空仓等待): {_bpe}")
            return 0  # Broker 不可用 → 不恢复任何仓位, 避免幽灵仓

        restored = 0

        # 手动持仓隔离: 恢复时跳过 owner='manual' 的持仓
        _manual_symbols = set()
        try:
            from app.core.config_container import get_all_manual_symbols
            _manual_symbols = get_all_manual_symbols()
        except Exception:
            pass

        from app.utils.numeric import safe_float as _sf

        for row in positions:
            try:
                symbol = row.get("symbol", "")
                if not symbol:
                    continue

                # 手动持仓隔离: 不纳入系统恢复
                if _manual_symbols and symbol in _manual_symbols:
                    logger.debug(f"[持仓恢复] 跳过手动持仓: {symbol}")
                    continue

                # ── P0 (2026-06-16): Broker-gated — 仅恢复 Broker 确认的持仓 ──
                if broker_positions_map and symbol not in broker_positions_map:
                    logger.warning(
                        f"[持仓恢复·Broker-gated] 跳过 {symbol} (DB 有记录但 Broker 无持仓)"
                    )
                    continue

                # P0 修复 (2026-05-21): 跳过 PRICE_FAIL 已关闭的期权，阻断同步死循环
                if symbol in _pf_closed:
                    logger.warning(f"[持仓恢复] 跳过 PRICE_FAIL 已关闭: {symbol}")
                    continue
                # 2026-07-16 P0 修复 (重启误平): 跳过恢复 playbook_sim 仓到主线 PositionStore。
                #   playbook 仓由 monitor_playbook_sim_exits 自管退出; 若恢复到主线 store 会被
                #   coordinator 锁为 owner='main' 并被主线 DEM 用 ATR_STOP 误平 (胜率杀手)。
                #   统一用 is_playbook_open_position 替代内联 SQL, 保证全链路判定一致。
                from app.playbook.pipeline import is_playbook_open_position
                if is_playbook_open_position(symbol):
                    logger.info(
                        f"[持仓恢复·playbook] 跳过 {symbol} (未平 PLAYBOOK_SIM 仓, 由 "
                        f"monitor_playbook_sim_exits 自管, 不纳入主线 PositionStore)"
                    )
                    continue
                direction_str = row.get("direction", "LONG")
                direction = Direction.SHORT if direction_str == "SHORT" else Direction.LONG
                
                # P0 修复 (2026-05-20): 期权仓位归主线策略管理
                is_option = any(x in symbol for x in ['26', '27', '28']) and any(x in symbol for x in ['P', 'C'])
                strategy_id = "voting_llm_option" if is_option else row.get("strategy_id", "recovered")
                
                position = Position(
                    strategy_id=strategy_id,
                    signal_id=row.get("signal_id", ""), trace_id=row.get("trace_id", ""),
                    symbol=symbol, direction=direction,
                    size=_sf(row.get("size", row.get("quantity"))),
                    avg_entry_price=_sf(row.get("avg_entry_price")),
                    initial_sl=_sf(row.get("initial_sl")),
                    initial_tp=_sf(row.get("initial_tp")),
                    current_sl=_sf(row.get("current_sl")),
                    take_profit_levels=row.get("take_profit_levels", []) or [],
                    time_stop_hours=int(row.get("time_stop_hours") or 168),
                    market_regime=MarketRegime.RANGE,
                    is_partially_reduced=bool(row.get("is_partially_reduced", False)),
                )
                entry_time_raw = row.get("entry_time")
                if entry_time_raw and isinstance(entry_time_raw, str):
                    position.entry_time = datetime.fromisoformat(entry_time_raw)
                elif isinstance(entry_time_raw, datetime):
                    position.entry_time = entry_time_raw
                else:
                    position.entry_time = now_et()
                position.trail_state = row.get("trail_state", {}) or {}
                # P0 (2026-07-02): 恢复 DB metadata 中的 protection 字段, 防止
                # owner=manual 但 source=auto_main 的持仓被 managed() 错误纳入
                # (metadata={} 丢失 protection → fallback MANAGED → exit_monitor 扫描 → DB拒绝 → 无限循环)
                position.metadata = {}
                try:
                    _raw_meta = row.get("metadata_json")
                    if _raw_meta:
                        import json as _json_meta
                        position.metadata = _json_meta.loads(_raw_meta) if isinstance(_raw_meta, str) else dict(_raw_meta)
                except Exception:
                    pass

                # P0 (2026-06-14): 从 trail_state_store 恢复 current_sl + peak
                try:
                    from app.risk.trail_state_store import get_trail_state_store
                    _ts = get_trail_state_store().load(symbol)
                    if _ts:
                        _ts_sl = float(_ts.get("current_sl", 0) or 0)
                        if _ts_sl > 0:
                            position.current_sl = _ts_sl
                        _ts_isl = float(_ts.get("initial_sl", 0) or 0)
                        if _ts_isl > 0 and position.initial_sl <= 0:
                            position.initial_sl = _ts_isl
                        for _k in ("highest_price", "lowest_price", "prev_scan_price",
                                   "last_trigger_time", "entry_time_ts", "entry_session"):
                            if _k in _ts:
                                position.trail_state[_k] = _ts[_k]
                except Exception as e:
                    logger.debug(f"[持仓恢复] trail_state 恢复失败 {symbol}: {e}")
                # P0 修复 (2026-05-20): 期权仓位设置正确的 metadata.source
                is_option = any(x in symbol for x in ['26', '27', '28']) and any(x in symbol for x in ['P', 'C'])
                if is_option:
                    position.metadata = {"source": "voting_llm_option", "is_option": True}
                # P0 修复 (2026-05-12): 恢复异常标记，防止重启后无限循环平仓
                # P0 修复 (2026-05-13): 自动恢复 — 当 Broker 确认持仓存在且 DB 一致时,
                #   清除 is_anomalous 并恢复正常监控 (之前是一旦标记就永久阻塞退出逻辑)
                if row.get("is_anomalous"):
                    broker_qty = None
                    if broker_positions_map and symbol in broker_positions_map:
                        broker_qty = broker_positions_map[symbol].get("qty")
                    if broker_qty is not None:
                        db_size = _sf(row.get("size", row.get("quantity")))
                        if abs(broker_qty - db_size) < 1:  # 允许 1 股舍入误差
                            # Broker 确认 + 数量一致 → 自动恢复
                            try:
                                scheduler.trade_store.unmark_anomalous(
                                    symbol,
                                    reason=f"broker_confirmed qty={broker_qty}"
                                )
                                logger.info(
                                    f"[持仓恢复·自动修复] {symbol} is_anomalous 已清除, "
                                    f"Broker 确认 {broker_qty} 股 = DB {db_size} 股, 恢复正常监控"
                                )
                            except Exception as e:
                                logger.warning(f"[持仓恢复·自动修复] {symbol} unmark_anomalous 失败: {e}")
                                # 降级: 仍然标记为阻塞
                                position.metadata["_close_blocked"] = True
                                position.metadata["_close_blocked_reason"] = f"DB is_anomalous=1, unmark failed: {e}"
                        else:
                            # Broker 有但数量不一致 → 仍然阻塞
                            position.metadata["_close_blocked"] = True
                            position.metadata["_close_blocked_reason"] = (
                                f"DB is_anomalous=1, Broker qty={broker_qty} ≠ DB {db_size}"
                            )
                            logger.warning(
                                f"[持仓恢复] {symbol} Broker/DB 数量不一致, 仍标记为异常"
                            )
                    else:
                        # Broker 无此持仓 → 仍标记为异常
                        position.metadata["_close_blocked"] = True
                        position.metadata["_close_blocked_reason"] = "DB is_anomalous=1 (历史平仓失败)"
                        position.metadata["_close_blocked_at"] = row.get("last_sync_at", "")
                        logger.warning(f"[持仓恢复] {symbol} 已标记为异常持仓, 跳过监控")
                # 源头打标 (2026-06-11): 确保 metadata.source 正确, 下游用 store.managed() 过滤
                from app.positions import tag_position, PositionSource
                _src = PositionSource.from_str(strategy_id)
                tag_position(position, _src)
                # 保留 is_anomalous 等运行时标记 (tag_position 不覆盖已有 metadata)
                if position.metadata.get("source") != _src.value:
                    position.metadata["source"] = _src.value
                scheduler.position_store.add(symbol, position)
                restored += 1
                logger.info(f"[持仓恢复] {symbol} {direction_str} {position.size}股 @${position.avg_entry_price:.2f}")
            except Exception as e:
                logger.warning(f"[持仓恢复] 恢复单条记录失败: {e}")
        logger.info(f"[持仓恢复] 完成：{restored}/{len(positions)} 条记录恢复")

        # 2026-07-01: 期权持仓恢复已删除 (自动交易不做期权)

        return restored
    except Exception as e:
        logger.warning(f"[持仓恢复] 查询 DB 失败: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════════
#  Task #5 (2026-06-15): 手动持仓安全网 (双链路并行)
#  - 手动持仓 (source == MANUAL): 不执行自动 SL/TP/trailing, 仅做安全网告警
#  - 自动持仓: 走原有 ExitMonitor / Dispatcher 完整退出流程
#  - 设计原则: 不干预手动决策, 仅在达到警戒线时通知
# ═══════════════════════════════════════════════════════════════════

_MANUAL_ALERT_COOLDOWN: Dict[tuple, float] = {}  # {(symbol, alert_type): last_alert_ts}
_MANUAL_ALERT_COOLDOWN_SECONDS = 3600  # 每标的每类型通知冷却 1 小时
_MANUAL_ALERT_LOCK = threading.Lock()


def _should_send_manual_alert(symbol: str, alert_type: str) -> bool:
    """检查是否应该发送手动持仓告警 (防刷屏冷却)"""
    try:
        key = (symbol, alert_type)
        now = time.time()
        with _MANUAL_ALERT_LOCK:
            last = _MANUAL_ALERT_COOLDOWN.get(key, 0)
            if now - last > _MANUAL_ALERT_COOLDOWN_SECONDS:
                _MANUAL_ALERT_COOLDOWN[key] = now
                return True
        return False
    except Exception:
        return False


def _is_pre_close_time() -> bool:
    """判断是否处于收盘前提醒时段 (15:50-16:00 ET)"""
    try:
        now_e = now_et()
        return now_e.hour == 15 and now_e.minute >= 50
    except Exception:
        return False


def _get_unrealized_pnl_pct(position, current_price: Optional[float] = None) -> Optional[float]:
    """计算持仓的未实现盈亏百分比 (LONG/SHORT 自适应)"""
    try:
        if current_price is None:
            current_price = getattr(position, "current_price", None)
        entry_price = getattr(position, "avg_entry_price", None)
        if entry_price is None:
            entry_price = getattr(position, "entry_price", None)
        if current_price is None or entry_price in (None, 0):
            meta = getattr(position, "metadata", None) or {}
            if isinstance(meta, dict):
                v = meta.get("unrealized_pnl_pct")
                if v is not None:
                    return float(v)
            return None
        entry_price = float(entry_price)
        current_price = float(current_price)
        if entry_price <= 0 or current_price <= 0:
            return None
        direction = getattr(position, "direction", None)
        dir_str = direction.value.upper() if hasattr(direction, "value") else str(direction).upper()
        if dir_str == "SHORT":
            return (entry_price - current_price) / entry_price
        return (current_price - entry_price) / entry_price
    except Exception:
        return None


def _send_manual_alert(message: str, position) -> None:
    """发送手动持仓预警通知 (复用钉钉, 异常兜底不影响主流程)"""
    try:
        logger.warning(f"[ManualSafetyNet] {message}")
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            DingTalkNotifier().send_text(message)
        except Exception as e:
            logger.debug(f"[ManualSafetyNet] 钉钉告警发送失败: {e}")
    except Exception as e:
        logger.warning(f"[ManualSafetyNet] Failed to send alert: {e}")


def _check_manual_safety_net(scheduler, position) -> None:
    """手动持仓安全网检查 — 不干预决策, 仅在收盘前发送提醒

    浮亏告警已由 ManualPositionGuard 独立线程覆盖 (5%/10% 分级 + 组合级),
    本函数仅保留收盘前提醒 (ManualPositionGuard 不含此功能).
    """
    try:
        symbol = getattr(position, "symbol", "?")

        # 收盘前提醒 (15:50 ET 之后)
        if _is_pre_close_time():
            if _should_send_manual_alert(symbol, "pre_close"):
                _send_manual_alert(
                    f"📢 收盘提醒: 手动持仓 {symbol} 仍在持有，是否需要平仓？",
                    position,
                )
    except Exception as e:
        try:
            sym = getattr(position, "symbol", "?")
        except Exception:
            sym = "?"
        logger.warning(f"[ManualSafetyNet] Error checking {sym}: {e}")


# ═══════════════════════════════════════════════════════════════════
#  持仓检查 (ExitMonitor 驱动)
# ═══════════════════════════════════════════════════════════════════

def _check_positions(scheduler):
    from app.core.scheduler_triggers import get_market_phase, MarketPhase
    phase = get_market_phase()
    if phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT):
        return
    if phase == MarketPhase.PRE_MARKET:
        return
    # P0 修复 (2026-05-14): 盘后时段 (AFTER_HOURS 16:00-20:00 ET) 禁止退出检查
    # 根因: 盘后流动性极低，价格数据不可靠，trailing stop 误触发风险高
    # 且 Futu 盘后下单可能无法成交或产生严重滑点
    if phase == MarketPhase.AFTER_HOURS:
        return

    # ── Task #5 (2026-06-15): 手动持仓安全网 (双链路并行) ──
    # 手动持仓不在 managed() 列表中, 但仍需安全网监控:
    # - 不执行自动 SL/TP/trailing
    # - 浮亏超 8% / 收盘前提醒
    # - 出错时不阻断主流程 (managed() 自动持仓继续走原有逻辑)
    try:
        from app.positions.source import PositionSource
        for _mp in scheduler.position_store.all():
            try:
                _meta = getattr(_mp, "metadata", None) or {}
                _src_raw = (_meta.get("source") if isinstance(_meta, dict) else None) \
                    or getattr(_mp, "owner_strategy", "") or ""
                _src = PositionSource.from_str(_src_raw)
                if _src.is_manual():
                    # 手动持仓: 仅安全网检查, 不进入自动退出流程
                    _check_manual_safety_net(scheduler, _mp)
            except Exception as e:
                logger.warning(
                    f"[DualMode] Failed to check position source for "
                    f"{getattr(_mp, 'symbol', '?')}: {e}"
                )
    except Exception as e:
        logger.debug(f"[ManualSafetyNet] Outer pass error: {e}")
    # ── 自动持仓: 沿用源头隔离, 走原有完整退出逻辑 ──
    positions = scheduler.position_store.managed()  # 源头隔离: 仅系统管理的仓位 (2026-06-11)
    if scheduler.polling_controller and positions:
        pos_info = {}
        for p in positions:
            meta = getattr(p, "metadata", {}) or {}
            if meta.get("is_option"): continue
            pos_info[p.symbol] = {"stop_loss": p.current_sl, "entry_price": p.avg_entry_price}
        at_risk = scheduler.polling_controller.check_stop_loss_risk_for_positions(pos_info)
        for risk in at_risk:
            logger.warning(f"[预估风控] {risk['symbol']} 预估触及止损 (上次价=${risk['last_price']:.2f} SL=${risk['stop_loss']:.2f} 概率={risk['risk_probability']:.0%})")
    monitored_positions = []
    # H2 (2026-06-03): scan+monitor 竞态保护 — 跳过刚开仓的 position
    # 如果 position 的 entry_time 在当前 monitor 周期内 (30s), 跳过退出评估
    # 防止 scan_loop 刚开的仓被 monitor_loop 同一轮误判退出
    _h2_now = now_et()
    _h2_grace_seconds = 30  # monitor 间隔 30s, 跳过本周期内开仓的 position
    # P0 Karpathy fix (2026-05-13): broker-synced 仓位不执行 exit_monitor 逻辑
    # Broker-first (2026-06-03): 不再跳过 broker-synced 仓位
    exit_monitor_for_filter = get_exit_monitor()
    for position in positions:
        meta = getattr(position, "metadata", {}) or {}
        if meta.get("is_option"): continue
        # Broker-first (2026-06-03): 不再跳过 broker-synced 仓位
        # 所有仓位统一进入退出监控，双重平仓防护在 _close_position 执行前校验
        if meta.get("_close_blocked"):
            # P0 修复 (2026-05-13, 增强 2026-06-16): 运行时自动恢复
            # 增强: 恢复前必须确认 broker 仍有持仓, 否则直接收敛移除
            last_attempt = meta.get("_close_recovery_attempt", "")
            now_key = now_et().strftime("%Y-%m-%d %H:")
            hour_bucket = now_et().strftime("%Y-%m-%d %H")
            if last_attempt != hour_bucket:
                meta["_close_recovery_attempt"] = hour_bucket
                # ── P0 (2026-06-16): 恢复前查 broker, 无仓则收敛 ──
                _broker_qty = None
                try:
                    from app.pipeline.order_executor import _refresh_broker_position_qty
                    _broker_qty = _refresh_broker_position_qty(scheduler, position.symbol)
                except Exception as e:
                    logger.debug(f"[幽灵收敛] broker qty 查询失败 {position.symbol}: {e}")
                if _broker_qty is not None and _broker_qty <= 0:
                    logger.warning(
                        f"[监控·幽灵收敛] {position.symbol} broker 已无持仓, "
                        f"直接从 PositionStore 移除 (不再恢复)"
                    )
                    try:
                        scheduler.trade_store.upsert_open_position(
                            position, source="scheduler", action="ghost_converged"
                        )
                    except Exception as e:
                        logger.debug(f"[幽灵收敛] DB upsert 失败 {position.symbol}: {e}")
                    scheduler.position_store.remove(position.symbol)
                    try:
                        from app.pipeline.order_executor import _clear_close_retry
                        _clear_close_retry(position.symbol, position.direction)
                    except Exception as e:
                        logger.debug(f"[幽灵收敛] clear_close_retry 失败 {position.symbol}: {e}")
                    continue
                logger.info(
                    f"[监控·自动恢复尝试] {position.symbol} 尝试清除 is_anomalous..."
                )
                try:
                    result = scheduler.trade_store.unmark_anomalous(
                        position.symbol, reason="runtime_self_heal"
                    )
                    meta["_close_blocked"] = False
                    meta["_close_blocked_reason"] = ""
                    monitored_positions.append(position)
                    logger.info(
                        f"[监控·自动恢复成功] {position.symbol} 已恢复监控 "
                        f"(unmark_result={result})"
                    )
                    continue
                except Exception as e:
                    logger.warning(f"[监控·自动恢复] {position.symbol} 异常: {e}")
            last_warn = meta.get("_close_warned_at", "")
            if last_warn != now_key:
                logger.warning(f"[监控] {position.symbol} 已标记为异常持仓, 跳过监控")
                meta["_close_warned_at"] = now_key
            continue
        # H2 (2026-06-03): scan+monitor 竞态保护
        _entry_time = getattr(position, 'entry_time', None)
        if _entry_time:
            try:
                _age_seconds = (_h2_now - _entry_time).total_seconds()
                if 0 <= _age_seconds < _h2_grace_seconds:
                    logger.debug(
                        f"[H2·竞态保护] {position.symbol} 刚开仓 {_age_seconds:.0f}s < {_h2_grace_seconds}s, "
                        f"跳过本轮退出评估"
                    )
                    continue
            except Exception as e:
                logger.debug(f"[H2·竞态保护] 检查异常 {position.symbol}: {e}")
        monitored_positions.append(position)
    if not monitored_positions: return
    from app.pipeline.order_executor import _get_current_price
    price_cache = {}
    for position in monitored_positions:
        current_price = _get_current_price(scheduler, position.symbol)
        # P2 fix (2026-05-01): Validate numeric type before caching (prevents dict from Push cache)
        if isinstance(current_price, (int, float)) and current_price > 0:
            price_cache[position.symbol] = current_price
            if scheduler.polling_controller:
                change_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
                scheduler.polling_controller.update_result(position.symbol, current_price, change_pct)

            # ─── MAE/MFE 跟踪 (2026-06-02: 反馈闭环修复) ───
            # 每个 monitor tick 更新最大不利/有利偏移, 平仓时写入 trade_records
            # 这是止损优化的核心数据: 如果 sl_hit 交易的 MAE 远小于 SL 距离,
            # 说明止损被噪声扫掉后价格回到目标方向 → SL 太紧
            try:
                _entry_price = position.avg_entry_price
                if _entry_price > 0 and position.size > 0:
                    if position.direction == Direction.LONG:
                        _unrealized_pnl = (current_price - _entry_price) * position.size
                    else:
                        _unrealized_pnl = (_entry_price - current_price) * position.size
                    # MAE: 取历史最小 (最大浮亏, 负数越小越差)
                    _prev_mae = getattr(position, 'max_adverse_excursion', 0.0) or 0.0
                    if _unrealized_pnl < _prev_mae:
                        position.max_adverse_excursion = _unrealized_pnl
                    # MFE: 取历史最大 (最大浮盈, 正数越大越好)
                    _prev_mfe = getattr(position, 'max_favorable_excursion', 0.0) or 0.0
                    if _unrealized_pnl > _prev_mfe:
                        position.max_favorable_excursion = _unrealized_pnl
            except Exception:
                pass  # MAE/MFE 跟踪失败不影响退出逻辑
    if not price_cache:
        metadata = getattr(scheduler, "_price_fail_metadata", {"count": 0, "last_alert": ""})
        metadata["count"] = metadata.get("count", 0) + 1
        setattr(scheduler, "_price_fail_metadata", metadata)
        if metadata["count"] >= 6 and metadata.get("last_alert") != now_et().strftime("%Y-%m-%d %H:"):
            logger.warning(f"[退出监控] 连续 {metadata['count']} 轮价格获取失败，所有持仓失去止损保护！")
            metadata["last_alert"] = now_et().strftime("%Y-%m-%d %H:")
            setattr(scheduler, "_price_fail_metadata", metadata)
        return
    if hasattr(scheduler, "_price_fail_metadata"):
        scheduler._price_fail_metadata = {"count": 0, "last_alert": ""}
    exit_monitor = get_exit_monitor()
    if not exit_monitor._initialized:
        try:
            if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
                adapter = scheduler.execution_engine.broker_adapter
                if adapter and adapter.is_connected() and adapter.quote_ctx:
                    exit_monitor.initialize(quote_ctx=adapter.quote_ctx)
        except Exception as e:
            logger.debug(f"[退出监控] ExitMonitor 初始化跳过: {e}")
    def _get_price(sym):
        return price_cache.get(sym)
    def _execute_close(pos, reason):
        from app.pipeline.order_executor import _close_position

        # P0 终极防护 (2026-06-15): DB 级手动持仓检查 — 不依赖内存状态
        # 根因: 进程未重启时, 内存中 Position 可能仍被错误分类为 auto_main
        # 直接查 DB + 手动审计, 确保手动仓位绝不被自动平仓
        try:
            _sym = getattr(pos, 'symbol', '')
            if _sym:
                from app.core.db_utils import get_main_db
                import sqlite3 as _sql3
                _conn = _sql3.connect(str(get_main_db()), timeout=3)

                # 检查 1: manual_order_audit 今日是否有手动订单
                try:
                    from app.utils.timezone import now_et as _now_et_pm
                    _today = _now_et_pm().strftime("%Y-%m-%d")
                except Exception:
                    _today = datetime.now().strftime("%Y-%m-%d")
                _manual_cnt = _conn.execute(
                    "SELECT COUNT(*) FROM manual_order_audit WHERE symbol=? AND timestamp LIKE ?",
                    (_sym, f"{_today}%")
                ).fetchone()[0]

                # 检查 2: open_positions.owner='manual'
                _owner_row = _conn.execute(
                    "SELECT owner FROM open_positions WHERE symbol=?", (_sym,)
                ).fetchone()
                _db_owner = (_owner_row[0] or '').lower() if _owner_row else ''

                _conn.close()

                if _manual_cnt > 0 or _db_owner == 'manual':
                    # 同步修正 DB owner + metadata.protection (根因修复 2026-07-02)
                    # 根因: guard 只修 owner='manual' 但 metadata.protection 仍为 'managed'
                    # → is_strategy_managed() 仍返回True → 退出检查持续触发 → 每轮HARD_STOP被拒绝
                    # 一条规则消灭: owner=manual 时同步 protection=protected, 从源头过滤
                    try:
                        import json as _json
                        _fix_conn = _sql3.connect(str(get_main_db()), timeout=3)
                        _row = _fix_conn.execute(
                            "SELECT metadata_json FROM open_positions WHERE symbol=?", (_sym,)
                        ).fetchone()
                        _meta = _json.loads(_row[0]) if _row and _row[0] else {}
                        _meta['protection'] = 'protected'
                        _fix_conn.execute(
                            "UPDATE open_positions SET owner='manual', metadata_json=? WHERE symbol=?",
                            (_json.dumps(_meta), _sym)
                        )
                        _fix_conn.commit()
                        _fix_conn.close()
                        # 同步更新内存 Position 对象 (本轮立即生效)
                        if hasattr(pos, 'metadata') and isinstance(pos.metadata, dict):
                            pos.metadata['protection'] = 'protected'
                        logger.warning(
                            f"[退出防护] {pos.symbol} owner→manual, protection→protected "
                            f"(手动审计={_manual_cnt}笔)"
                        )
                    except Exception as e:
                        logger.debug(f"[退出防护] DB 修正失败 {pos.symbol}: {e}")
                    logger.warning(
                        f"[退出防护] {pos.symbol} 拒绝自动平仓: "
                        f"手动审计={_manual_cnt}笔, DB owner={_db_owner}"
                    )
                    return False
        except Exception as _guard_err:
            # ── P1 (2026-06-16): FAIL CLOSED — 防护检查异常时拒绝平仓 ──
            # 根因: 原逻辑 catch 后继续执行平仓, 等于防护形同虚设
            logger.error(
                f"[退出防护·FAIL_CLOSED] {getattr(pos, 'symbol', '?')} "
                f"DB 检查异常, 拒绝自动平仓: {_guard_err}"
            )
            return False

        try:
            reason_map = {"FLASH_EXIT": ExitReason.FLASH_EXIT, "HARD_STOP": ExitReason.HARD_STOP,
                "ATR_STOP": ExitReason.TRAILING_STOP, "CHANDELIER": ExitReason.TRAILING_STOP,
                "BREAKEVEN": ExitReason.BREAKEVEN_STOP, "TIME_STOP": ExitReason.TIME_STOP,
                "DAY_TRADE_TP": ExitReason.DAY_TRADE_TP,
                "DAY_TRADE_TIMEOUT": ExitReason.DAY_TRADE_TIMEOUT,
                "DAY_TRADE": ExitReason.DAY_TRADE_TIMEOUT,  # fallback
                "SCALE_OUT": ExitReason.SCALE_OUT, "PRE_CLOSE_EXIT": ExitReason.PRE_CLOSE_EXIT,
                "PRE_CLOSE_EXIT_PARTIAL": ExitReason.PRE_CLOSE_EXIT,
                "TECHNICAL_BREACH": ExitReason.STRUCTURE_EXIT,
                # RFC-061 (2026-06-24): 新增退出原因正确归因 (否则 fallback 到 TRAILING_STOP)
                "MAX_HOLD_T5": ExitReason.MAX_HOLD_T5, "TIME_EXIT": ExitReason.TIME_EXIT,
                "SL_ATR_STOP": ExitReason.TRAILING_STOP, "EOD_FORCE_CLOSE": ExitReason.TIME_STOP,
                "OVERNIGHT_GAP_STOP": ExitReason.OVERNIGHT_GAP_STOP}
            er = reason_map.get(reason.get("reason", ""), ExitReason.TRAILING_STOP)
            cp = reason.get("exit_price", pos.avg_entry_price)
            close_qty = reason.get("close_qty", 0)
            if close_qty > 0 and close_qty < pos.size:
                _partial_close_position(scheduler, pos, cp, close_qty, er)
            else:
                _close_position(scheduler, pos, cp, er)
            # P0 修复 (2026-05-12): _close_position 内部吞异常，不抛给调用方。
            # 改为检查 metadata 中的 _close_blocked 标记判断是否真正成功。
            meta = getattr(pos, "metadata", {}) or {}
            if meta.get("_close_blocked"):
                logger.error(f"[退出监控] {pos.symbol} 平仓被阻断: {meta.get('_close_blocked_reason', 'unknown')}")
                return False
            # P2 修复 (2026-05-08): 退出后设置信号冷却，防止同一标的立即被重新开仓
            # 根因: QCOM 05-07 单日 15 笔 SHORT，累计 -5.08% — 退出后无冷却导致反复进出
            try:
                scheduler._set_cooldown(pos.symbol)
                logger.info(f"[退出冷却] {pos.symbol} 设置信号冷却 ({getattr(scheduler, 'SIGNAL_COOLDOWN_MINUTES', 60)}min)")
            except Exception as ce:
                logger.debug(f"[退出冷却] 设置失败 {pos.symbol}: {ce}")
            # H3 (2026-06-03): 设置退出屏障 (线程安全)
            _set_exit_barrier(pos.symbol)
            logger.debug(f"[退出屏障] {pos.symbol} 设置 {_EXIT_BARRIER_SECONDS}s 入场冷却")
            # P0 修复 (2026-06-02): FLASH_EXIT 时触发系统级熔断计数
            if reason.get("reason", "") == "FLASH_EXIT":
                try:
                    from app.pipeline.order_executor import record_flash_exit
                    record_flash_exit()
                except Exception as e:
                    logger.debug(f"[FLASH_EXIT] record_flash_exit 失败: {e}")
            # P0 修复 (2026-06-02): 更新标的日亏损计数 (防死亡螺旋)
            try:
                from app.pipeline.order_executor import _update_symbol_daily_loss
                dir_str = pos.direction.value if hasattr(pos.direction, 'value') else str(pos.direction)
                if dir_str == "LONG":
                    est_pnl = (cp - pos.avg_entry_price) * pos.size
                else:
                    est_pnl = (pos.avg_entry_price - cp) * pos.size
                _update_symbol_daily_loss(pos.symbol, est_pnl)
            except Exception as e:
                logger.debug(f"[日亏损计数] _update_symbol_daily_loss 失败 {pos.symbol}: {e}")
            return True
        except Exception as e:
            logger.error(f"[退出监控] {pos.symbol} 平仓执行失败: {e}")
            return False
    policy_guardian = None
    try:
        from app.risk.exit_policy_guardian import get_guardian
        policy_guardian = get_guardian()
    except Exception: pass
    earnings_guard = None
    try:
        from app.risk.earnings_risk_guard import EarningsRiskGuard
        earnings_guard = EarningsRiskGuard()
    except Exception: pass

    # ============================================================
    # 手动仓位隔离已在源头完成: position_store.managed() (2026-06-11)
    # MANUAL/LEGACY/UNKNOWN source → PROTECTED → 不进入 positions 列表
    # ============================================================

    # ============================================================
    # Phase 4 (2026-05-31): ExitRuleDispatcher 主路径路由
    #   - dispatcher_enabled=True & shadow_mode=False 时先走 dispatcher
    #   - 已平仓的 symbol 从 monitored_positions 中移除避免重复触发
    #   - dispatcher 异常且 fallback_on_error=True 时 → 全量交 legacy 处理
    # ============================================================
    controller_cfg = _get_exit_controller_config()
    dispatcher_enabled = bool(controller_cfg.get("dispatcher_enabled", False))
    shadow_mode_ctrl = bool(controller_cfg.get("shadow_mode", True))
    fallback_on_error = bool(controller_cfg.get("fallback_on_error", True))
    log_suppressed = bool(controller_cfg.get("log_suppressed_rules", True))
    monitor_comparison = bool(controller_cfg.get("monitor_comparison", False))

    dispatcher_closed_symbols = set()
    if dispatcher_enabled:
        dispatcher = _build_exit_dispatcher(scheduler)
        if dispatcher is None:
            if not fallback_on_error:
                logger.error("[ExitDispatcher] 不可用且 fallback_on_error=False, 跳过本轮")
                return
            logger.warning("[ExitDispatcher] 不可用，回退到 legacy 路径")
        else:
            for position in list(monitored_positions):
                # 2026-06-01: 过滤 orb_engine 仓位 — ORBEngine 自管退出 (ORBExitStack)
                # orb_engine 有独立的4层退出栈 (FloorStop/Trailing/TP/EOD), DEM 不得干预
                # orb_pipeline 仓位正常由 DEM 管理 (无独立退出栈)
                # 2026-06-24: 也过滤 playbook_sim/live 仓位 — monitor_playbook_sim_exits 自管退出
                _orb_owner = getattr(position, "owner_strategy", "") or ""
                if _orb_owner == "orb_engine":
                    continue
                if _orb_owner in ("playbook_sim", "playbook_live", "playbook_shadow"):
                    continue
                try:
                    cur_price = price_cache.get(position.symbol)
                    if not cur_price or cur_price <= 0:
                        continue
                    pos_dict = _build_dispatcher_position_dict(position)
                    md_dict = _build_dispatcher_market_data(scheduler, position, cur_price)
                    trail_state = getattr(position, "trail_state", {}) or {}
                    decision = dispatcher.evaluate(pos_dict, md_dict, trail_state)
                    if not decision or not decision.should_exit:
                        continue

                    # 互斥压制日志
                    if log_suppressed and decision.suppressed_rules:
                        logger.info(
                            f"[ExitDispatcher·互斥] {position.symbol} 激活={decision.rule_name} "
                            f"压制={decision.suppressed_rules}"
                        )

                    # ── RFC-057 (2026-06-01): grace period 过滤 ──
                    # 数据驱动 (n=60): <30min 持仓占总亏损 188% (-$27,437)
                    # grace 期内仅放行 flash_exit / circuit_breaker / earnings_guard / pre_close
                    # 浮亏 >= max_grace_loss_pct 时立即解除保护 (硬底兜底)
                    try:
                        grace_block_reason = _check_grace_period_block(
                            position, cur_price, decision.rule_name
                        )
                        if grace_block_reason:
                            logger.info(
                                f"[grace_period·RFC057] {position.symbol} 屏蔽 {decision.rule_name} "
                                f"({grace_block_reason})"
                            )
                            continue
                    except Exception as _gpe:
                        # grace 检查异常时不阻断主流程 (保守: 让退出正常执行)
                        logger.debug(f"[grace_period] 检查异常 (忽略): {_gpe}")

                    if shadow_mode_ctrl:
                        # Shadow 模式: 仅记录决策, 不实际执行
                        logger.info(
                            f"[ExitDispatcher·Shadow] {position.symbol} 本应触发: "
                            f"rule={decision.rule_name} reason={decision.reason}"
                        )
                        continue

                    # 正式模式: 执行平仓
                    reason_dict = _build_exit_reason_dict(position, decision, cur_price)
                    if _execute_close(position, reason_dict):
                        # H3 (2026-06-03): 设置退出屏障 (线程安全)
                        _set_exit_barrier(position.symbol)
                        # P0 (2026-06-02): 更新标的日亏损 (防死亡螺旋)
                        try:
                            from app.pipeline.order_executor import _update_symbol_daily_loss
                            _ep = float(getattr(position, 'avg_entry_price', 0) or 0)
                            _sz = float(getattr(position, 'size', 0) or 0)
                            _dir = getattr(position, 'direction', None)
                            _dir_str = _dir.value if hasattr(_dir, 'value') else str(_dir)
                            if _ep > 0 and _sz > 0:
                                if _dir_str == "LONG":
                                    _pnl = (cur_price - _ep) * _sz
                                else:
                                    _pnl = (_ep - cur_price) * _sz
                                _update_symbol_daily_loss(position.symbol, _pnl)
                        except Exception as e:
                            logger.debug(f"[日亏损计数] exit path _update_symbol_daily_loss 失败 {position.symbol}: {e}")
                        # Check if this was a partial close (position still exists)
                        _is_partial_close = reason_dict.get("close_qty", 0) > 0
                        if _is_partial_close:
                            # Partial close: set cooldown to prevent re-fire in legacy monitor this tick.
                            # The position still exists, so we add it to dispatcher_closed_symbols
                            # only for the current tick; the next tick it will be rescanned normally.
                            if hasattr(exit_monitor, '_exit_cooldowns'):
                                exit_monitor._exit_cooldowns[position.symbol] = time.time()
                            # Update trail state with trigger time to prevent legacy re-fire
                            if isinstance(trail_state, dict):
                                trail_state["last_trigger_time"] = time.time()
                            dispatcher_closed_symbols.add(position.symbol)
                        else:
                            dispatcher_closed_symbols.add(position.symbol)
                        logger.warning(
                            f"[ExitDispatcher·执行] {position.symbol} | rule={decision.rule_name} | "
                            f"price=${reason_dict['exit_price']} | {decision.reason}"
                            f"{' | PARTIAL' if _is_partial_close else ''}"
                        )
                        if monitor_comparison:
                            logger.info(
                                f"[ExitDispatcher·对比] {position.symbol} dispatcher触发={decision.rule_name} | "
                                f"同轮 legacy 将在 scan_positions 跳过该仓位"
                            )
                except Exception as ee:
                    logger.error(
                        f"[ExitDispatcher] {getattr(position, 'symbol', '?')} 评估/执行异常: {ee}",
                        exc_info=True,
                    )
                    if not fallback_on_error:
                        # 严格模式: 不 fallback, 跳过该仓位继续下一个
                        continue
                    logger.warning(
                        f"[ExitDispatcher] {getattr(position, 'symbol', '?')} 异常已忽略，该仓位交 legacy 处理"
                    )

    # 已被 dispatcher 平仓的仓位不再交 legacy，避免重复触发
    if dispatcher_closed_symbols:
        monitored_positions = [p for p in monitored_positions if p.symbol not in dispatcher_closed_symbols]

    try:
        closed = exit_monitor.scan_positions(positions=monitored_positions, get_price_fn=_get_price,
            execute_close_fn=_execute_close, policy_guardian=policy_guardian, earnings_guard=earnings_guard)
        if closed:
            for c in closed:
                _pnl = c.get('pnl_pct', 0.0)
                logger.warning(f"[退出执行] {c['symbol']} | {c['reason']} | PnL={_pnl*100:.1f}%")
    except Exception as e:
        logger.error(f"[退出监控] 扫描异常: {e}", exc_info=True)
    try:
        if hasattr(scheduler, "trade_store") and scheduler.trade_store:
            import hashlib as _hl
            # 系统仓位: 已有 trail 更新
            _all_trail = list(monitored_positions)
            for pos in _all_trail:
                if not pos.trail_state: continue
                _stable = {k: v for k, v in pos.trail_state.items() if k != "prev_scan_price"}
                _stable["current_sl"] = getattr(pos, "current_sl", 0)
                current_hash = _hl.md5(json.dumps(_stable, sort_keys=True).encode()).hexdigest()
                last_hash = getattr(pos, "_last_trail_hash", None)
                if current_hash != last_hash:
                    # Fix: preserve original source instead of overwriting with "exit_monitor"
                    _pos_src = (getattr(pos, 'metadata', None) or {}).get('source', 'auto_main')
                    scheduler.trade_store.upsert_open_position(pos, source=_pos_src, action="update_trail")
                    pos._last_trail_hash = current_hash
    except Exception as e:
        logger.debug(f"[退出监控] trail_state DB 同步失败: {e}")


# ── P0 (2026-06-15): 手动持仓识别 — 防止运行时对账纳入自动管理 ──
_manual_position_cache: dict = {}
_manual_position_cache_ts: float = 0
_MANUAL_CACHE_TTL = 120  # 2 min cache


def _is_manual_position_for_reconcile(symbol: str) -> bool:
    """检查仓位是否为手动开仓 (三层检查 + 2min 缓存).

    SNDK/MU 事件修复: 运行时 broker 对账每 5min 调用,
    必须在 add 到 position_store 前排除手动仓位.
    """
    import time as _t
    now = _t.time()
    if symbol in _manual_position_cache and (now - _manual_position_cache_ts) < _MANUAL_CACHE_TTL:
        return _manual_position_cache[symbol]

    result = False

    # 1. ManualHoldRegistry (内存)
    try:
        from app.coordinator.manual_hold_registry import manual_hold_registry
        if manual_hold_registry.is_auto_blocked(symbol):
            result = True
    except Exception:
        pass

    # 2. DB open_positions.owner = 'manual'
    if not result:
        try:
            from app.core.db_utils import get_main_db
            import sqlite3 as _sql3
            _conn = _sql3.connect(str(get_main_db()), timeout=5)
            _row = _conn.execute(
                "SELECT owner, trader_type, strategy_id FROM open_positions WHERE symbol=? LIMIT 1",
                (symbol,)
            ).fetchone()
            _conn.close()
            if _row:
                owner = (_row[0] or "").lower()
                trader_type = (_row[1] or "").upper()
                strat = (_row[2] or "").lower()
                if owner == "manual":
                    result = True
                elif trader_type == "MANUAL" and not any(
                    s in strat for s in ("auto_", "main_pipeline", "voting_llm", "orb")
                ):
                    result = True
        except Exception:
            pass

    # 3. manual_order_audit (今日有手动订单)
    if not result:
        try:
            from app.core.db_utils import get_main_db
            import sqlite3 as _sql3
            from datetime import datetime as _dt
            _conn = _sql3.connect(str(get_main_db()), timeout=5)
            try:
                from app.utils.timezone import now_et as _now_et_pm2
                _today = _now_et_pm2().strftime("%Y-%m-%d")
            except Exception:
                _today = _dt.now().strftime("%Y-%m-%d")
            _cnt = _conn.execute(
                "SELECT COUNT(*) FROM manual_order_audit WHERE symbol=? AND timestamp LIKE ?",
                (symbol, f"{_today}%")
            ).fetchone()[0]
            _conn.close()
            if _cnt > 0:
                result = True
        except Exception:
            pass

    _manual_position_cache[symbol] = result
    _manual_position_cache_ts = now
    return result


def _reconcile_is_system_owned(symbol: str) -> bool:
    """未知持仓采纳前置检查: 是否有本系统开仓的证据.

    [SAFETY 2026-07-14] 旧逻辑依赖 _is_manual_position_for_reconcile 将手动仓排除,
    但那依赖手动仓被打标; 若用户在 sim 账户直接下未打标手动单, 旧逻辑会将其
    采纳为 broker_reconcile 仓 → ExitMonitor 可能自动平掉手动仓. 改为白名单式:
    仅采纳能证明是系统所开的仓, 其余一律只告警不纳管.

    系统所有权证据 (任一):
      - open_positions 有该标的且非手动标记 (owner!=manual 且 trader_type!=MANUAL)
      - 未平 trade_records 有该标的且 source_type ∈ (PLAYBOOK_SIM/LIVE/ORB_ENGINE)
    无证据 → 视为外部/手动仓, 调用方只告警不纳管.
    """
    try:
        from app.core.db_utils import get_main_db
        import sqlite3 as _s3
        _c = _s3.connect(str(get_main_db()), timeout=5)
        _row = _c.execute(
            "SELECT owner, trader_type FROM open_positions WHERE symbol=? LIMIT 1",
            (symbol,),
        ).fetchone()
        _c.close()
        if _row:
            _owner = (_row[0] or "").lower()
            _ttype = (_row[1] or "").upper()
            if _owner != "manual" and _ttype != "MANUAL":
                return True  # 系统台账有记录且非手动 → 系统仓
    except Exception:
        pass
    try:
        from app.core.paths import get_trade_records_db
        import sqlite3 as _s3
        _c = _s3.connect(str(get_trade_records_db()), timeout=5)
        _row = _c.execute(
            "SELECT 1 FROM trade_records WHERE symbol=? "
            "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE','ORB_ENGINE') "
            "AND (exit_time IS NULL OR exit_time='') LIMIT 1",
            (symbol,),
        ).fetchone()
        _c.close()
        if _row:
            return True
    except Exception:
        pass
    return False


def _reconcile_position_store(scheduler):
    """P0: 强制 Broker 对账并同步到 PositionStore

    P0 修复 (2026-05-01): 复用 USPositionSync 的 Futu 连接，不再每次创建/关闭 OpenSecTradeContext。
    之前每次调用都新建连接 → query → close()，导致 CallClose 日志刷屏 + 浪费 OpenD 槽位.
    """
    from app.core.models import Position as Pos, Direction as Dir, MarketRegime as MR
    
    # 1. 查询 Broker 实际持仓
    try:
        from app.core.position_sync import get_us_position_sync
        syncer = get_us_position_sync()
        broker_positions = syncer.query_broker_positions()
    except Exception as e:
        logger.debug(f"[持仓对账] Broker 查询失败: {e}")
        return
    
    # 2. 获取内存 PositionStore
    mem_positions = list(scheduler.position_store.all())
    mem_symbols = set(p.symbol for p in mem_positions)

    broker_symbols = set(broker_positions.keys()) if broker_positions else set()

    # P0 修复 (2026-05-23): 加载 PRICE_FAIL 已关闭集合，防止期权重导入循环
    _pf_closed = _load_price_fail_closed_set()

    # ── 对账分析 ──
    discrepancies = []

    # 3a. Broker 有但内存无 → 未知新持仓
    new_symbols = broker_symbols - mem_symbols
    for symbol in new_symbols:
        # P0 修复 (2026-05-23): 跳过 PRICE_FAIL 已关闭的期权，与 position_sync 保持一致
        if symbol in _pf_closed:
            logger.warning(f"[持仓对账] 跳过 PRICE_FAIL 已关闭期权 {symbol} (防止重导入循环)")
            continue

        # P0 修复 (2026-06-15): 跳过手动持仓 — 防止运行时对账将手动仓位纳入自动管理
        # SNDK/MU 事件根因: broker 对账每 5min 运行, 不检查手动标记直接 add 到 position_store
        if _is_manual_position_for_reconcile(symbol):
            logger.info(f"[持仓对账] 跳过手动持仓 {symbol} (不纳入自动管理)")
            continue

        # 2026-07-16: 跳过 Playbook 管理的仓位 — 防止 broker 对账将 Playbook 仓
        # 重新纳管为 broker_reconcile → 主线 DEM 接管 → 双重平仓.
        from app.playbook.pipeline import is_playbook_open_position
        if is_playbook_open_position(symbol):
            logger.debug(f"[持仓对账·playbook] 跳过 {symbol} (未平 PLAYBOOK_SIM 仓, 由 monitor_playbook_sim_exits 自管)")
            continue

        bp = broker_positions[symbol]

        # 完全未知的新持仓 → 可能是漏记，也可能是 Futu 结算延迟
        # P0 修复 (2026-05-11): 检查是否刚刚被系统平仓（Futu 结算延迟窗口）
        from datetime import datetime, timedelta
        RECENT_CLOSE_WINDOW_MINUTES = 10
        try:
            from app.storage.trade_store import TradeStore
            from app.core.db_utils import get_main_db
            db_path = str(get_main_db())
            ts = TradeStore(db_path)
            conn = ts._get_conn()
            cutoff = (now_et() - timedelta(minutes=RECENT_CLOSE_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
            recent_exits = conn.execute(
                "SELECT COUNT(*) FROM trade_records WHERE symbol=? AND exit_time>=? AND exit_reason IS NOT NULL",
                (symbol, cutoff)
            ).fetchone()[0]
                # 也检查 open_positions 表是否有同标的 recent close 标记
            recent_pos_closes = conn.execute(
                "SELECT COUNT(*) FROM open_positions WHERE symbol=? AND sync_status='closed' AND updated_at>=?",
                (symbol, cutoff)
            ).fetchone()[0]
            if recent_exits > 0 or recent_pos_closes > 0:
                logger.info(
                    f"[持仓对账] {symbol}: Broker 有持仓但本地无记录，但 {RECENT_CLOSE_WINDOW_MINUTES}min 内"
                    f"有退出记录（{recent_exits}+{recent_pos_closes}），判定为 Futu 结算延迟，跳过创建"
                )
                continue
        except Exception as check_err:
            logger.debug(f"[持仓对账] {symbol}: 冷却期检查失败，继续按新持仓处理: {check_err}")

        # [SAFETY 2026-07-14] 外部仓保护: 采纳未知持仓前须有本系统开仓证据, 否则只告警不纳管.
        #   根因: layer2(_is_manual_position_for_reconcile) 依赖手动仓被打标; sim 账户里未打标
        #   的手动单会被旧逻辑采纳为 broker_reconcile → ExitMonitor 可能自动平掉手动仓.
        if not _reconcile_is_system_owned(symbol):
            logger.warning(
                f"[持仓对账·外部仓保护] {symbol} broker 有持仓但无本系统开仓证据 → "
                f"视为外部/手动仓, 只告警不纳入自动管理 ({bp['direction']} {bp['qty']}股 @${bp['cost']:.2f})"
            )
            discrepancies.append({
                "type": "external_unmanaged_position",
                "symbol": symbol,
                "direction": bp['direction'],
                "size": bp['qty'],
                "cost": bp['cost'],
            })
            continue

        direction = Dir.SHORT if bp['direction'] == 'SHORT' else Dir.LONG
        cost, size = bp['cost'], bp['qty']
        sl_mult = 1.08 if bp['direction'] == 'SHORT' else 0.92
        
            # P0 修复 (2026-05-11): 仓位集中度门禁 — 防止单只持仓超限
        from app.core.config_container import get_config
        try:
            cfg = get_config()
            max_pos_pct = cfg.get_dotted("risk.position.max_position_pct", 0.15)
            account_equity = 0.0
            try:
                from app.risk.capital_safety import get_capital_safety_system
                cs = get_capital_safety_system()
                account_equity = getattr(cs._state, 'current_equity', 1000000)
            except Exception:
                account_equity = float(cfg.get_dotted("capital.fixed_amount", 82_000))  # SSOT: YAML=82000
            position_value = size * cost
            position_pct = position_value / account_equity if account_equity > 0 else 999
            if position_pct > max_pos_pct:
                logger.critical(
                    f"[持仓对账·超限] {symbol} ({bp['direction']}) 仓位 {position_pct:.0%} "
                    f"超过上限 {max_pos_pct:.0%} (${position_value:,.0f} / ${account_equity:,.0f})。"
                    f"恢复真实持仓但标记告警，建议立即减仓。"
                )
        except Exception as e:
            logger.warning(f"[持仓对账·集中度检查] 失败 (放行): {e}")

            # RFC-055 修复 (2026-06-01): 从 open_positions 恢复 trace_id/signal_id
            # 防止 broker 对账重建持仓时丢失归因链路
        recovered_trace_id = ""
        recovered_signal_id = ""
        try:
            from app.core.paths import get_trade_records_db
            import sqlite3 as _sql3
            _conn = _sql3.connect(str(get_trade_records_db()))
            _row = _conn.execute(
                "SELECT trace_id, signal_id FROM open_positions WHERE symbol=? ORDER BY updated_at DESC LIMIT 1",
                (symbol,)
            ).fetchone()
            if _row and _row[0]:
                recovered_trace_id = _row[0]
                recovered_signal_id = _row[1] or ""
                logger.info(f"[持仓对账] {symbol}: 恢复 trace_id={recovered_trace_id}")
            _conn.close()
        except Exception as e:
            logger.debug(f"[持仓对账] {symbol}: 恢复 trace_id 失败 (放行): {e}")

        position = Pos(strategy_id="broker_reconcile", symbol=symbol, direction=direction, size=size,
            avg_entry_price=cost, initial_sl=cost*sl_mult, current_sl=cost*sl_mult,
            initial_tp=cost*(0.85 if bp['direction']=='SHORT' else 1.15),
            take_profit_levels=[], time_stop_hours=72, market_regime=MR.RANGE,
            trace_id=recovered_trace_id, signal_id=recovered_signal_id)
        position.trail_state = {"highest_price": cost, "lowest_price": cost, "prev_scan_price": cost}
        scheduler.position_store.add(symbol, position)
        logger.warning(f"[持仓对账] 发现 Broker 新持仓 {symbol} ({bp['direction']} {size}股 @${cost:.2f})")
        discrepancies.append({
            "type": "unknown_position",
            "symbol": symbol,
            "direction": bp['direction'],
            "size": size,
            "cost": cost,
        })
    
    # 3b. 内存有但 Broker 无 → 区分外部平仓 vs 真幽灵仓
    # 根因修复 (2026-06-16): 与 position_sync 同一逻辑 — 查 broker 今日成交
    ghost_symbols = mem_symbols - broker_symbols
    try:
        from app.core.position_sync import get_us_position_sync
        _ps = get_us_position_sync()
        option_symbols = _ps._get_option_position_symbols()
        _pm_today_orders = getattr(_ps, '_today_orders', {})
    except Exception:
        option_symbols = set()
        _pm_today_orders = {}
    cleaned = 0
    for symbol in ghost_symbols:
        if symbol in option_symbols:
            continue

        if symbol in mem_symbols:
            existing_pos = scheduler.position_store.get(symbol)
            scheduler.position_store.remove(symbol)
            _remove_from_open_positions_db(symbol)
            cleaned += 1
            
            # 检查是否为外部平仓 (broker 今日有平仓方向的成交)
            _deal = _pm_today_orders.get(symbol)
            _is_ext_close = False
            if _deal and existing_pos:
                _trd = str(_deal.get('trd_side', '')).upper()
                _dir = existing_pos.direction.name if hasattr(existing_pos.direction, 'name') else str(existing_pos.direction)
                if (_dir == 'LONG' and 'SELL' in _trd) or (_dir == 'SHORT' and 'BUY' in _trd):
                    _is_ext_close = True
            
            if _is_ext_close and existing_pos:
                _exit_p = float(_deal.get('price', 0))
                _entry_p = existing_pos.avg_entry_price
                _size = existing_pos.size
                _dir_name = existing_pos.direction.name if hasattr(existing_pos.direction, 'name') else str(existing_pos.direction)
                if _dir_name == 'LONG':
                    _pnl = (_exit_p - _entry_p) * _size
                    _pnl_pct = (_exit_p - _entry_p) / _entry_p if _entry_p > 0 else 0
                else:
                    _pnl = (_entry_p - _exit_p) * _size
                    _pnl_pct = (_entry_p - _exit_p) / _entry_p if _entry_p > 0 else 0
                
                logger.info(f"[持仓对账] 外部平仓 {symbol} (出${_exit_p:.2f}, PnL=${_pnl:.2f})")
                discrepancies.append({
                    "type": "external_close",
                    "symbol": symbol,
                    "source": "main_pipeline",
                    "exit_price": _exit_p,
                    "pnl": round(_pnl, 2),
                })
            else:
                logger.info(f"[持仓对账] 清理真幽灵仓 {symbol} (无今日成交记录)")
                # 真幽灵不进 discrepancies，不触发告警
    
    # 3c. 共同持仓 → 检查数量/成本价差异
    common_symbols = mem_symbols & broker_symbols
    for symbol in common_symbols:
        bp = broker_positions[symbol]
        existing = scheduler.position_store.get(symbol)
        if existing is None: continue
        if abs(existing.size - bp['qty']) > 0.1 or abs(existing.avg_entry_price - bp['cost']) > 0.01:
            existing.size = bp['qty']
            existing.avg_entry_price = bp['cost']
            sl_mult = 1.08 if bp['direction'] == 'SHORT' else 0.92
            existing.initial_sl = bp['cost'] * sl_mult
            if not existing.current_sl or existing.current_sl == 0: existing.current_sl = bp['cost'] * sl_mult
            existing.trail_state = existing.trail_state or {}
            existing.trail_state["prev_scan_price"] = bp['cost']
            logger.info(f"[持仓对账] 更新 {symbol}: size={bp['qty']}, cost=${bp['cost']:.2f}")
    
    # ── 告警推送 ──
    if discrepancies:
        _send_reconcile_alert(scheduler, discrepancies, broker_positions, mem_positions)
    
    if new_symbols or cleaned:
        logger.info(f"[持仓对账] 完成: +{len(new_symbols)} 新增, -{cleaned} 清理, 内存共 {scheduler.position_store.count()} 个")


def _remove_from_open_positions_db(symbol: str):
    """从 open_positions DB 移除指定标的"""
    try:
        import sqlite3
        from app.core.db_utils import get_main_db
        db_path = get_main_db()
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("DELETE FROM open_positions WHERE symbol=?", (symbol,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug(f"[持仓移除] DB DELETE 失败 {symbol}: {e}")


def _send_reconcile_alert(scheduler, discrepancies: list, broker_positions: dict,
                          mem_positions: list):
    """对账发现差异时推送钉钉/飞书告警"""
    try:
        # 统计
        unknown = [d for d in discrepancies if d['type'] == 'unknown_position']
        ghosts = [d for d in discrepancies if d['type'] == 'ghost_cleaned']
        external = [d for d in discrepancies if d['type'] == 'external_unmanaged_position']

        broker_count = len(broker_positions)
        mem_count = len(mem_positions)

        lines = [
            f"### [FTSeek-US] 🔍 持仓对账告警\n\n",
            f"**📊 对账摘要**:\n\n",
            f"- 券商实际持仓: {broker_count} 只\n\n",
            f"- 内存 PositionStore: {mem_count} 只\n\n",
        ]

        if unknown:
            lines.append(f"**🔴 未知新持仓 (需人工确认)**:\n\n")
            for d in unknown:
                lines.append(f"- {d['symbol']} {d['direction']} {d['size']}股 @${d['cost']:.2f}\n\n")

        if external:
            lines.append(f"**🟡 外部/手动仓 (已保护·未纳入自动管理)**:\n\n")
            for d in external:
                lines.append(f"- {d['symbol']} {d['direction']} {d['size']}股 @${d['cost']:.2f}\n\n")

        if ghosts:
            lines.append(f"**⚪ 已清理幽灵仓**:\n\n")
            for d in ghosts:
                lines.append(f"- {d['symbol']} (Main)\n\n")

        lines.append(f"> 时间: {now_et().strftime('%Y-%m-%d %H:%M')}")

        if scheduler.notifier:
            scheduler.notifier.send_text("\n".join(lines))
            logger.info(f"[持仓对账] 告警已推送: {len(unknown)}未知 + {len(ghosts)}幽灵")
    except Exception as e:
        logger.warning(f"[持仓对账] 告警推送失败: {e}")


def _update_capital_safety(scheduler):
    """更新资本安全水位状态"""
    try:
        from app.risk.capital_safety import get_capital_safety_system
        capital = get_capital_safety_system()
        account_info = {}
        if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
            adapter = getattr(scheduler.execution_engine, "broker_adapter", None)
            if adapter and adapter.is_connected():
                try: account_info = adapter.get_account_info()
                except Exception as e: logger.debug(f"[资本安全] 富途账户查询失败: {e}")
        total_assets = account_info.get("total_assets", 0)
        cash = account_info.get("cash", 0)
        market_val = account_info.get("market_val", 0)
        buying_power = account_info.get("buying_power", 0)
        if total_assets <= 0:
            # Broker API 不可用时，使用上次已知现金 + 实际持仓市值
            positions = scheduler.position_store.all()
            market_val = sum(p.size * p.avg_entry_price for p in positions if p.avg_entry_price > 0)
            # P2 修复 (2026-04-29): 用上次已知现金 + 实际市值，而非错误的 initial + mv*0.1
            last_cash = capital._state.cash if capital._state.cash > 0 else capital._state.initial_capital
            cash = last_cash
            total_assets = cash + market_val
            logger.debug(f"[资本安全] Broker 数据不可用，估算: cash=${cash:,.0f} mv=${market_val:,.0f} equity=${total_assets:,.0f}")
        state = capital.update_account(current_equity=total_assets, cash=cash, market_value=market_val, buying_power=buying_power)
        report = capital.get_status_report()
        is_first = not hasattr(scheduler, '_last_water_level')
        level_changed = is_first or state.water_level.value != getattr(scheduler, '_last_water_level', '')
        if level_changed:
            logger.info(f"[资本安全] {report['water_emoji']} {report['water_label']} | 净值=${report['current_equity']:,.0f} | 回撤 {report['drawdown_pct']:.1f}% | 风险系数 {report['risk_multiplier']}")
        if not hasattr(scheduler, '_last_water_level'): scheduler._last_water_level = state.water_level.value
        if state.water_level.value != scheduler._last_water_level:
            old_level = scheduler._last_water_level
            scheduler._last_water_level = state.water_level.value
            if scheduler.notifier and state.water_level.value in ('yellow','orange','red','blackout'):
                emojis = {"yellow":"🟡","orange":"🟠","red":"🔴","blackout":"⚫"}
                labels = {"yellow":"警戒","orange":"警告","red":"危险","blackout":"熔断"}
                scheduler.notifier.send_text(f"{emojis.get(state.water_level.value,'')} FTSeek-US 资本水位变化\n旧水位: {old_level} → 新水位: {labels.get(state.water_level.value,'')}\n当前净值: ${report['current_equity']:,.0f}\n历史高点: ${report['peak_equity']:,.0f}\n当前回撤: {report['drawdown_pct']:.1f}%")
                if state.water_level.value == 'blackout': logger.critical(f"[资本安全] 熔断触发: {state.lock_reason}")
    except Exception as e:
        logger.warning(f"[资本安全] 更新失败: {e}")


# ═══════════════════════════════════════════════════════════════════
#  待确认订单检查 (P0 根因修复 2026-05-14)
# ═══════════════════════════════════════════════════════════════════

def _check_pending_orders(scheduler):
    """
    检查所有待确认的订单状态
    
    职责:
    1. 检查未确认订单的成交状态
    2. 盘后订单在开盘时重新评估
    3. 超时订单标记为 STALE
    4. 确认成交后执行持仓清理和通知
    """
    try:
        from app.risk.order_tracker import get_order_tracker, OrderState
        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        
        tracker = get_order_tracker()
        phase = get_market_phase()
        
        # 获取所有待处理的订单
        pending = tracker.get_pending_orders()
        if not pending:
            return
        
        logger.info(f"[订单追踪] 检查 {len(pending)} 个待确认订单 (phase={phase.value})")
        
        for order in pending:
            futu_id = order["futu_order_id"]
            symbol = order["symbol"]
            state = order["state"]
            
            # 盘后订单，等待开盘后处理
            if state == OrderState.AFTER_HOURS_PENDING.value:
                if phase == MarketPhase.TRADING:
                    logger.info(f"[订单追踪] {symbol} 盘后订单现在开盘，重新检查状态")
                    # 重新检查订单状态
                    _process_pending_order(scheduler, tracker, order)
                continue
            
            # 交易时段检查订单状态
            if phase == MarketPhase.TRADING:
                _process_pending_order(scheduler, tracker, order)
            elif state in (OrderState.PENDING_CONFIRM.value, OrderState.SUBMITTED.value):
                # 非交易时段检查超时 (Task #37: 传入 broker_adapter 供超时自动撤单)
                _broker = None
                try:
                    if scheduler.execution_engine and scheduler.execution_engine.broker_adapter:
                        _broker = scheduler.execution_engine.broker_adapter
                except Exception:
                    _broker = None
                tracker.check_stale_orders(broker_adapter=_broker)
                
    except Exception as e:
        logger.warning(f"[订单追踪] 检查待确认订单失败: {e}")


def _process_pending_order(scheduler, tracker, order: dict):
    """处理单个待确认订单"""
    try:
        from app.risk.order_tracker import OrderState
        from app.pipeline.order_executor import _notify_exit, _close_position
        from app.core.models import ExitReason
        from futu import RET_OK as RET_OK_FUTU, TrdEnv
        
        futu_id = order["futu_order_id"]
        symbol = order["symbol"]
        direction = order["direction"]
        quantity = order["quantity"]
        exit_reason_str = order.get("exit_reason", "UNKNOWN")
        meta = order.get("metadata", {})
        
        # 查询订单状态
        if not scheduler.execution_engine or not scheduler.execution_engine.broker_adapter:
            logger.warning(f"[订单追踪] {symbol} 无 broker_adapter，跳过")
            return
        
        trd_env = TrdEnv.SIMULATE if getattr(scheduler, 'trade_env', 1) == 1 else TrdEnv.REAL
        ret, data = scheduler.execution_engine.broker_adapter.trade_ctx.order_list_query(
            order_id=futu_id,
            trd_env=trd_env
        )
        
        if ret != RET_OK_FUTU or data.empty:
            logger.warning(f"[订单追踪] {symbol} 查询订单 {futu_id} 失败: ret={ret}")
            return
        
        status = str(data.iloc[0].get('order_status', ''))
        logger.info(f"[订单追踪] {symbol} futu_id={futu_id} 状态={status}")
        
        if 'FILLED' in status or 'DEALT' in status:
            # 订单已成交，执行持仓清理和通知
            logger.info(f"[订单追踪] {symbol} 订单已成交，执行平仓清理")
            tracker.update_order_state(futu_id, OrderState.FILLED, {"futu_status": status})
            
            # 获取持仓信息
            position = scheduler.position_store.get(symbol)
            if position:
                # 获取当前价格
                from app.pipeline.order_executor import _get_current_price
                current_price = meta.get("close_current_price", _get_current_price(scheduler, symbol))
                if current_price <= 0:
                    current_price = position.avg_entry_price  # fallback
                
                # 计算盈亏
                if position.direction.value == "LONG":
                    pnl = (current_price - position.avg_entry_price) * position.size
                    pnl_pct = (current_price - position.avg_entry_price) / position.avg_entry_price
                else:
                    pnl = (position.avg_entry_price - current_price) * position.size
                    pnl_pct = (position.avg_entry_price - current_price) / position.avg_entry_price
                
                # 执行平仓清理
                position.exit_time = now_et()
                position.exit_price = current_price
                position.exit_reason = ExitReason.__members__.get(exit_reason_str, ExitReason.STOP_LOSS)
                position.realized_pnl = pnl
                
                # 从持仓中移除
                scheduler.position_store.remove(symbol)
                
                # 写入交易记录
                try:
                    if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
                        # P0 修复 (2026-05-22): 从 position 和 trade_attribution 回填 signal_strength
                        _sig_strength = 0
                        _sig_confidence = 0.0
                        _owner = getattr(position, "owner_strategy", None) or "main"  # P0 (2026-06-05): 直接从position读
                        _strategy = getattr(position, "strategy_id", "") or _owner  # P0 (2026-06-05): 真实策略优先
                        try:
                            _pos_meta = getattr(position, "metadata", {}) or {}
                            _sig_strength = _pos_meta.get("signal_strength", 0)
                            _sig_confidence = _pos_meta.get("signal_confidence", 0.0)
                            _owner = getattr(position, "owner_strategy", None) or _owner
                            _strategy = _pos_meta.get("strategy_id", "") or getattr(position, "strategy_id", "") or _strategy
                            # Fallback: 从 trade_attribution DB 回填
                            if _sig_strength == 0 and position.trace_id:
                                from app.pipeline.order_executor import _fetch_attribution_data
                                _sig_strength, _sig_confidence = _fetch_attribution_data(
                                    position.trace_id,
                                    symbol=symbol,
                                    trade_date=_et_trade_date(position.entry_time),
                                )
                        except Exception as e:
                            logger.debug(f"[归因] _fetch_attribution_data 失败 {symbol}: {e}")
                        scheduler.trade_store.save_trade({
                            "trace_id": position.trace_id,
                            "trade_date": _et_trade_date(position.entry_time),
                            "symbol": symbol,
                            "direction": position.direction.value,
                            "entry_time": position.entry_time.isoformat(),
                            "exit_time": position.exit_time.isoformat(),
                            "entry_price": position.avg_entry_price,
                            "exit_price": current_price,
                            "quantity": position.size,
                            "realized_pnl": pnl,
                            "pnl_pct": pnl_pct,
                            "exit_reason": exit_reason_str,
                            "holding_hours": position.holding_hours,
                            "signal_strength": _sig_strength,
                            "signal_confidence": _sig_confidence,
                            "futu_order_id": getattr(position, "futu_order_id", ""),
                            "close_futu_order_id": futu_id,
                            "strategy_id": _strategy,
                            "owner_strategy": _owner,
                        })
                except Exception as e:
                    logger.warning(f"[订单追踪] {symbol} 交易记录写入失败: {e}")
                
                # 发送通知（只在确认成交后发送）
                try:
                    _notify_exit(scheduler, position, position.exit_reason or ExitReason.STOP_LOSS, pnl, pnl_pct)
                    logger.info(f"[订单追踪] {symbol} 平仓通知已发送")
                except Exception as e:
                    logger.warning(f"[订单追踪] {symbol} 通知发送失败: {e}")
            else:
                logger.warning(f"[订单追踪] {symbol} 持仓已不存在，跳过清理")
                
        elif 'FAIL' in status or 'DEL' in status or 'CANCEL' in status:
            # 订单失败/被拒绝
            logger.warning(f"[订单追踪] {symbol} 订单被拒绝，状态={status}")
            tracker.update_order_state(futu_id, OrderState.FAILED, {"futu_status": status})

            # 清除 pending 标记，让持仓继续被监控
            position = scheduler.position_store.get(symbol)
            if position:
                meta = getattr(position, "metadata", {}) or {}
                # P0 (2026-06-15): 递增拒绝计数, 防止"持仓不足"等 broker 拒绝导致无限重试
                reject_count = meta.get("_close_reject_count", 0) + 1
                meta["_close_reject_count"] = reject_count
                if reject_count >= 2:
                    # 连续 2 次被拒绝 → 锁定, 不再自动重试
                    meta["_close_blocked"] = True
                    meta["_close_blocked_reason"] = f"broker 连续 {reject_count} 次拒绝"
                    meta["_close_blocked_at"] = time.time()
                    logger.warning(
                        f"[订单追踪] {symbol} 平仓被锁定: broker 连续 {reject_count} 次拒绝 "
                        f"(不再自动重试, 需人工介入)"
                    )
                meta.pop("_close_order_submitted", None)
                meta.pop("_close_futu_order_id", None)
                meta.pop("_close_order_unconfirmed", None)
                # P0 (2026-06-12): 同步清理 _close_in_progress, 允许重试
                meta.pop("_close_in_progress", None)
                meta.pop("_close_in_progress_ts", None)
                meta.pop("_close_order_failed", None)
                meta.pop("_close_order_status", None)
                position.metadata = meta
                if reject_count < 2:
                    logger.info(f"[订单追踪] {symbol} 订单失败，持仓继续监控 (已解锁, 拒绝计数={reject_count})")
                
        elif 'SUBMITTING' in status or 'SUBMITTED' in status or 'WAITING' in status:
            # 仍在排队
            logger.info(f"[订单追踪] {symbol} 订单仍在排队，状态={status}")
            tracker.update_order_state(futu_id, OrderState.PENDING_CONFIRM, {"futu_status": status})
            
    except Exception as e:
        logger.warning(f"[订单追踪] {symbol} 处理订单失败: {e}")


def monitor_loop(scheduler):
    """持仓监控主循环 (集成资本安全水位追踪)"""
    from app.core.scheduler_triggers import get_market_phase, MarketPhase
    last_capital_update = 0
    last_subscription_check = 0
    last_broker_reconcile = 0
    # last_coordinator_reconcile removed (2026-05-18): duplicate of T6 reconcile
    last_order_tracker_gc = 0  # P0-5 (2026-05-14)

    # P2-8 (2026-05-14): watchdog 注册 + 启动
    # P0-X (2026-05-15): 加 auto_ping (基于昨日 scan_loop 1802s 误报告警)
    try:
        from app.observability.watchdog import get_watchdog
        wd = get_watchdog()
        _interval_sec = scheduler.MONITOR_INTERVAL_MINUTES * 60
        wd.register("monitor_loop", stale_threshold_sec=_interval_sec * 5)
        wd.register("scan_loop", stale_threshold_sec=scheduler.SCAN_INTERVAL_MINUTES * 60 * 3)
        wd.start(check_interval_sec=60.0)
        # auto_ping: 只要 scheduler.running, 每 30s 自动 ping (不依赖循环顶部)
        wd.auto_ping_when_alive(
            "scan_loop", lambda: getattr(scheduler, "running", False), interval_sec=30
        )
        wd.auto_ping_when_alive(
            "monitor_loop", lambda: getattr(scheduler, "running", False), interval_sec=30
        )
    except Exception as _wde:
        logger.debug(f"[Watchdog] 启动失败 (降级): {_wde}")
        wd = None
    # _coord_reconcile_interval removed (2026-05-18): duplicate of T6 reconcile
    while scheduler.running:
        try:
            # P2-8: 心跳
            try:
                if wd is not None:
                    wd.ping("monitor_loop")
            except Exception:
                pass
            phase = get_market_phase()
            if phase in (MarketPhase.WEEKEND, MarketPhase.OVERNIGHT):
                for _ in range(int(scheduler.MONITOR_INTERVAL_MINUTES * 60)):
                    if not scheduler.running: return
                    time.sleep(1)
                continue
            now = time.time()
            if now - last_broker_reconcile >= 300:
                last_broker_reconcile = now
                try: _reconcile_position_store(scheduler)
                except Exception as e: logger.debug(f"[持仓对账] 异常: {e}")
            # M7 (2026-06-03): 定期检查 Push 数据健康状态, 数据停止时主动告警
            if now - getattr(monitor_loop, '_last_push_health_check', 0) >= 120:
                monitor_loop._last_push_health_check = now
                try:
                    _futu = getattr(scheduler, 'data_router', None)
                    if _futu and hasattr(_futu, 'futu') and _futu.futu:
                        _futu.futu.ensure_healthy()
                except Exception:
                    pass
            # P0-R2 (2026-05-14): 推送 SystemCircuitBreaker 状态到 PreTradeChecker
            try:
                _eng = getattr(scheduler, "risk_engine", None)
                _sb = getattr(_eng, "system_breaker", None) if _eng else None
                if _sb:
                    _state = "TRIGGERED" if _sb.is_triggered() else (
                        "DEGRADED" if (hasattr(_sb, "is_degraded") and _sb.is_degraded()) else "NORMAL"
                    )
                    _ck = getattr(_eng, "pre_trade_checker", None) if _eng else None
                    if _ck and hasattr(_ck, "update_breaker_state"):
                        _ck.update_breaker_state(_state)
            except Exception as _bre:
                logger.debug(f"[SystemBreaker push] 异常: {_bre}")
            # P1-8 (2026-05-14): Eastmoney 新闻源 symbols 与 watchlist 同步
            try:
                wl = getattr(scheduler, "watchlist", None)
                if wl:
                    # 提取 ticker (支持 dict / str / Stock 对象)
                    symbols = []
                    for item in wl:
                        if isinstance(item, str):
                            symbols.append(item.replace("US.", "").upper())
                        elif isinstance(item, dict):
                            sym = item.get("symbol") or item.get("code") or item.get("ticker")
                            if sym:
                                symbols.append(str(sym).replace("US.", "").upper())
                        elif hasattr(item, "symbol"):
                            symbols.append(str(item.symbol).replace("US.", "").upper())
                    # news_pipeline 内部 source list, 找 EastmoneyUSSource
                    if hasattr(scheduler, "news_pipeline_v2") and scheduler.news_pipeline_v2:
                        for src in getattr(scheduler.news_pipeline_v2, "_sources", []) or []:
                            if hasattr(src, "update_symbols") and getattr(src, "source_id", "") == "eastmoney_us":
                                src.update_symbols(symbols)
                                break
            except Exception as _ee:
                logger.debug(f"[Eastmoney·sync] 异常: {_ee}")

            # P0-5 (2026-05-14): OrderTracker 每日 GC
            if now - last_order_tracker_gc >= 86400:
                last_order_tracker_gc = now
                try:
                    from app.risk.order_tracker import get_order_tracker
                    get_order_tracker().gc_stale_orders(max_age_hours=24)
                except Exception as e:
                    logger.debug(f"[OrderTracker·GC] 异常: {e}")
            # Phase A (2026-05-14): coordinator 账本/锁与 PositionStore 对账
            # DISABLED (2026-05-18): Duplicate of T6 reconcile (scheduler_core.py:778-816).
            # Both ran on offset 150s intervals, causing capital oscillation:
            # T6 reads positions correctly (by_strategy={'main': 0.451}),
            # but this path reads EMPTY positions (by_strat_pct={}) → resets allocations to 0.0.
            # T6 alone is sufficient for periodic reconcile.
            # if now - last_coordinator_reconcile >= _coord_reconcile_interval:
            #     last_coordinator_reconcile = now
            #     try:
            #         _coord = getattr(scheduler, "coordinator", None)
            #         if _coord is not None and _coord.enabled:
            #             _coord.reconcile(scheduler.position_store)
            #             snap = _coord.capital.snapshot()
            #             logger.info(
            #                 f"[Coordinator·reconcile] capital={snap.by_strategy} "
            #                 f"total={snap.total_reserved:.3f}/{snap.max_total:.2f} "
            #                 f"locks={_coord.lock.all_locked()}"
            #             )
            #     except Exception as e:
            #         logger.debug(f"[Coordinator·reconcile] 异常: {e}")
            if now - last_subscription_check >= 600:
                last_subscription_check = now
                try: scheduler._ensure_futu_disconnected_during_hk()
                except Exception as e: logger.debug(f"[动态订阅] 检查异常: {e}")
            if scheduler._is_market_open():
                now = time.time()
                if now - last_capital_update >= 300:
                    try:
                        _update_capital_safety(scheduler)
                        last_capital_update = now
                    except Exception as e: logger.warning(f"[资本安全] 更新异常: {e}")
            # P2 修复 (2026-04-29): 非交易时段也更新资金状态 (盘前/盘后持仓仍存在)
            else:
                now = time.time()
                if now - last_capital_update >= 600:
                    try:
                        _update_capital_safety(scheduler)
                        last_capital_update = now
                    except Exception as e: logger.debug(f"[资本安全] 非交易时段更新异常: {e}")
            if scheduler.position_store.count() > 0:
                _check_positions(scheduler)
            # P0 根因修复 (2026-05-14): 检查待确认订单状态
            _check_pending_orders(scheduler)
        except Exception as e:
            logger.error(f"[监控] 异常：{e}", exc_info=True)
        time.sleep(scheduler.MONITOR_INTERVAL_MINUTES * 60)
