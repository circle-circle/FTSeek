"""
订单执行管线 — FTSeek-US 下单执行与仓位管理

从 scheduler_tasks.py 拆分而来。本模块包含完整的订单构建、风控门禁、
仓位计算、平仓逻辑和通知。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from app.utils.timezone import _et_trade_date, now_et
from app.storage.trade_store import assign_cohort

from app.core.models import (
    SignalScore, Direction, MarketRegime,
    Order, OrderType, OrderState, Position,
    TradeEvent, ExitReason, AlertLevel,
)

from app.data.adaptive_polling import StockTier
from app.storage.rejection_logger import get_rejection_logger
from app.execution.twap_executor import (
    TWAPExecutor, TWAPResult, should_use_twap, cancel_active_twap,
)
from app.execution.symbol_lock_manager import get_symbol_lock_manager
from app.risk.pdt_guard import PDTGuard

logger = logging.getLogger(__name__)

# ── 全局常量 ──
DEFAULT_ORDER_QTY = 50  # 默认下单股数 (watchlist_us.json 可覆盖)

_pdt_guard: Optional[PDTGuard] = None

def _get_pdt_guard() -> PDTGuard:
    """懒加载并复用 PDTGuard 实例, 避免 import 时访问 DB."""
    global _pdt_guard
    if _pdt_guard is None:
        _pdt_guard = PDTGuard()
    return _pdt_guard


def _with_symbol_lock_release(func):
    """装饰器: 保证 _close_position 任何 early return/异常都释放 SymbolLock."""
    from functools import wraps

    @wraps(func)
    def wrapper(scheduler, position, *args, **kwargs):
        try:
            return func(scheduler, position, *args, **kwargs)
        finally:
            try:
                _dir_str = position.direction.value if hasattr(position.direction, "value") else str(position.direction)
                _owner = getattr(position, "owner_strategy", None) or "main"
                get_symbol_lock_manager().release(position.symbol, _dir_str, _owner)
            except Exception as _sre:
                logger.debug(f"[SymbolLock·symdir·close] finally 释放异常: {_sre}")

    return wrapper


# ══════════════════════════════════════════════════════════════════
#  Task #34 (P0, 2026-06-16): 平仓重入与持仓不足级联失败修复
# ──────────────────────────────────────────────────────────────────
#  问题根因:
#    - 21:30:20 下单 success+订单ID, 21:30:29 立即"持仓不足"
#    - 23:07:01 "不支持交叉交易, 有待成交订单" — 旧订单未清算
#    - 重试无 backoff, 每 tick 重触发, 6 次失败 → is_anomalous → 8h 不可平仓
#  修复策略:
#    1. 平仓前强制刷新 broker.get_position(symbol)
#    2. 检测待成交订单 (broker.get_open_orders), 等待结算最多 60s
#    3. 指数退避 (2 次→5min, 3 次→30min, 5 次→永久阻塞+告警)
#    4. 退避计数按 (symbol, direction) 维度独立维护
#    5. 提供 manual_unblock(symbol) 后端方法供 Dashboard 解锁
# ══════════════════════════════════════════════════════════════════

# (symbol, direction) → {fail_count, cooldown_until, last_reason, last_failed_at, blocked}
_close_retry_state: Dict[Tuple[str, str], Dict[str, Any]] = {}

# 2026-06-29: 删除 SQLite 持久化 (_CLOSE_RETRY_TABLE / _get_retry_db_path / _ensure_retry_table /
# _load_close_retry_state / _persist_close_retry_state) — 进程内dict足够,
# 重启后position_sync重新同步仓位, 退避计数重置为0是合理行为.

# IC 门禁缓存 — 同一进程周期内 get_ic_summary() 只算一次 (避免 N symbols × SQL 查询)
_ic_summary_cache: Dict[str, Any] = {}
_ic_summary_cache_ts: float = 0.0
_IC_CACHE_TTL_SEC = 300.0  # 5 分钟

# 触发退避的错误关键字
_CLOSE_BACKOFF_ERR_PATTERNS = (
    "持仓不足", "insufficient", "not enough",
    "待成交订单", "未完成订单", "pending order",
    "不支持交叉交易",
)

# 退避阶梯: fail_count → cooldown_minutes (None=永久阻塞)
_CLOSE_BACKOFF_LADDER = {
    2: 5,
    3: 30,
    4: 30,
    5: None,  # 5 次 → 永久阻塞 + 告警
}

_CLOSE_BLOCK_THRESHOLD = 5  # 达到此次数 → block + 推送告警

# [ADR-015] owner_strategy → source_type 派生表 (2026-07-14)
# 平仓记录若漏传 source_type, trade_store 默认 MAIN_PIPELINE → 主线路径平掉
# playbook/orb 仓时归因误标. 此表让 close-side 按持仓真实 owner 落正确来源。
_OWNER_SOURCE_TYPE = {
    "playbook_sim": "PLAYBOOK_SIM",
    "playbook_live": "PLAYBOOK_LIVE",
    "playbook_shadow": "PLAYBOOK_SHADOW",
    "orb_engine": "ORB_ENGINE",
}


def _close_retry_key(symbol: str, direction) -> Tuple[str, str]:
    """生成 (symbol, direction) 的退避计数键"""
    _dir = direction.value if hasattr(direction, "value") else str(direction)
    return (str(symbol), str(_dir).upper())


def _is_backoff_error(err_msg: str) -> bool:
    """判断错误信息是否属于触发退避的类别 (持仓不足/待成交订单)"""
    if not err_msg:
        return False
    _msg = str(err_msg).lower()
    for _pat in _CLOSE_BACKOFF_ERR_PATTERNS:
        if _pat in err_msg or _pat.lower() in _msg:
            return True
    return False


def _check_close_cooldown(symbol: str, direction) -> Optional[str]:
    """检查 (symbol, direction) 当前是否在冷却中, 返回剩余原因字符串或 None"""

    _key = _close_retry_key(symbol, direction)
    _state = _close_retry_state.get(_key)
    if not _state:
        return None
    if _state.get("blocked"):
        return f"已永久阻塞 (fails={_state.get('fail_count', 0)}, reason={_state.get('last_reason', '')})"
    _cd_until = _state.get("cooldown_until")
    if _cd_until:
        try:
            _now = now_et()
            # now_et() 和 cooldown_until 均为 ET timezone-aware, 直接比较
            if _cd_until > _now:
                _remain = (_cd_until - _now).total_seconds()
                return f"冷却中 (剩余 {int(_remain)}s, fails={_state.get('fail_count', 0)})"
        except Exception as _e:
            logger.debug(f"[平仓·退避] 冷却时间比较异常: {_e}")
    return None


def _record_close_failure(
    scheduler,
    position,
    err_msg: str,
    is_backoff_category: bool = True,
) -> Tuple[int, Optional[int], bool]:
    """记录一次平仓失败, 推进退避阶梯.

    Returns:
        (fail_count, cooldown_minutes_or_none, is_blocked)
    """

    _key = _close_retry_key(position.symbol, position.direction)
    _state = _close_retry_state.setdefault(_key, {
        "fail_count": 0, "cooldown_until": None,
        "last_reason": "", "last_failed_at": None, "blocked": False,
    })
    _state["fail_count"] = _state.get("fail_count", 0) + 1
    _state["last_reason"] = str(err_msg)[:300]
    _state["last_failed_at"] = now_et()
    fail_count = _state["fail_count"]

    cooldown_min: Optional[int] = None
    blocked = False
    if is_backoff_category:
        cooldown_min = _CLOSE_BACKOFF_LADDER.get(
            fail_count,
            _CLOSE_BACKOFF_LADDER.get(min(fail_count, 4), None),
        )
    else:
        if fail_count >= 3:
            cooldown_min = 1

    if fail_count >= _CLOSE_BLOCK_THRESHOLD:
        blocked = True
        _state["blocked"] = True
        _state["cooldown_until"] = None
    elif cooldown_min:
        _start = now_et()
        _state["cooldown_until"] = _start + timedelta(minutes=cooldown_min)
        logger.warning(
            f"[平仓·退避] {position.symbol} {_key[1]} fails={fail_count} → 冷却 {cooldown_min}min "
            f"({_start.isoformat()} → {_state['cooldown_until'].isoformat()}) reason={str(err_msg)[:120]!r}"
        )


    return fail_count, cooldown_min, blocked


def _clear_close_retry(symbol: str, direction) -> None:
    """平仓成功后清零 (symbol, direction) 退避计数"""

    _key = _close_retry_key(symbol, direction)
    if _key in _close_retry_state:
        _state = _close_retry_state.pop(_key)
        if _state.get("fail_count", 0) > 0:
            logger.info(
                f"[平仓·退避] {symbol} {_key[1]} 平仓成功 → 清零退避计数 "
                f"(累计 fails={_state.get('fail_count', 0)})"
            )
    


def _refresh_broker_position_qty(scheduler, symbol: str) -> Optional[float]:
    """强制查询 broker, 返回该 symbol 的当前持仓数量 (绝对值).

    返回 None 表示查询失败 (无法判定); 0.0 表示 broker 已无持仓.
    """
    try:
        _engine = getattr(scheduler, "execution_engine", None)
        _adapter = getattr(_engine, "broker_adapter", None) if _engine else None
        if not _adapter or not hasattr(_adapter, "get_positions"):
            return None
        _positions = _adapter.get_positions() or []
        for _p in _positions:
            _sym = _p.get("symbol") if isinstance(_p, dict) else getattr(_p, "symbol", None)
            if _sym == symbol:
                _qty = _p.get("qty", 0) if isinstance(_p, dict) else getattr(_p, "qty", 0)
                try:
                    return abs(float(_qty))
                except Exception:
                    return 0.0
        return 0.0  # broker 列表中无此 symbol → 持仓为 0
    except Exception as _e:
        logger.debug(f"[平仓·broker持仓刷新] {symbol} 异常: {_e}")
        return None


def _query_broker_open_orders(scheduler, symbol: str) -> List[Dict[str, Any]]:
    """查询 broker 端该 symbol 的待成交订单 (status 为 SUBMITTED/SUBMITTING/WAITING_SUBMIT).

    返回 [{"order_id", "status", "qty", "dealt_qty"}, ...]; 失败返回 [].
    """
    try:
        _engine = getattr(scheduler, "execution_engine", None)
        _adapter = getattr(_engine, "broker_adapter", None) if _engine else None
        if not _adapter or not getattr(_adapter, "trade_ctx", None):
            return []
        from futu import TrdEnv, RET_OK as _RET_OK
        _trd_env = TrdEnv.SIMULATE if getattr(_adapter, "trade_env", 1) == 1 else TrdEnv.REAL
        _ret, _data = _adapter.trade_ctx.order_list_query(
            code=symbol,
            trd_env=_trd_env,
            acc_id=int(_adapter.account_id) if getattr(_adapter, "account_id", None) else 0,
        )
        if _ret != _RET_OK or _data is None or _data.empty:
            return []
        _open: List[Dict[str, Any]] = []
        # 富途未结订单状态
        _open_status_keys = ("SUBMITTED", "SUBMITTING", "WAITING_SUBMIT")
        for _idx in range(len(_data)):
            _row = _data.iloc[_idx]
            _status = str(_row.get("order_status", "")).upper()
            if any(_k in _status for _k in _open_status_keys):
                _open.append({
                    "order_id": str(_row.get("order_id", "")),
                    "status": _status,
                    "qty": float(_row.get("qty", 0) or 0),
                    "dealt_qty": float(_row.get("dealt_qty", 0) or 0),
                })
        return _open
    except ImportError:
        return []
    except Exception as _e:
        logger.debug(f"[平仓·open_orders] {symbol} 查询异常: {_e}")
        return []


def _wait_or_cancel_open_orders(
    scheduler, symbol: str, timeout_sec: int = 60, poll_interval_sec: int = 5,
) -> Tuple[bool, List[str]]:
    """若存在未完成订单, 轮询等待最多 timeout_sec 秒; 超时则取消未完成订单.

    Returns:
        (是否安全可发新单, 已取消订单列表)
    """
    _open = _query_broker_open_orders(scheduler, symbol)
    if not _open:
        return True, []
    logger.warning(
        f"[平仓·待成交] {symbol} 检测到 {len(_open)} 个未结订单, "
        f"等待结算最多 {timeout_sec}s (poll={poll_interval_sec}s)"
    )
    _waited = 0
    while _waited < timeout_sec:
        time.sleep(poll_interval_sec)
        _waited += poll_interval_sec
        _open = _query_broker_open_orders(scheduler, symbol)
        if not _open:
            logger.info(f"[平仓·待成交] {symbol} 未结订单已结算 (耗时 {_waited}s)")
            return True, []
    # 超时, 取消旧订单
    _canceled: List[str] = []
    _adapter = getattr(getattr(scheduler, "execution_engine", None), "broker_adapter", None)
    for _od in _open:
        _oid = _od.get("order_id", "")
        if not _oid or not _adapter:
            continue
        try:
            if _adapter.cancel_order(_oid):
                _canceled.append(_oid)
                logger.warning(
                    f"[平仓·待成交] {symbol} 超时取消旧订单 {_oid} "
                    f"(status={_od.get('status', '')})"
                )
        except Exception as _ce:
            logger.warning(f"[平仓·待成交] {symbol} 取消订单 {_oid} 失败: {_ce}")
    if _canceled:
        # P1-追加 (2026-06-16): 取消后验证 — 等待 broker 完成清算 (原 1s 不足)
        # Futu cancel 到清算通常需 3-5s, 用 poll_interval 做验证轮询
        _verify_waited = 0
        _verify_max = min(poll_interval_sec * 3, 15)  # 最多等 15s
        while _verify_waited < _verify_max:
            time.sleep(poll_interval_sec)
            _verify_waited += poll_interval_sec
            _remaining = _query_broker_open_orders(scheduler, symbol)
            if not _remaining:
                logger.info(
                    f"[平仓·待成交] {symbol} 取消后验证通过 ({_verify_waited}s), "
                    f"已取消 {len(_canceled)} 单"
                )
                return True, _canceled
        # 验证超时 (仍有未结订单), 警告但仍返回 (调用方会再尝试)
        logger.warning(
            f"[平仓·待成交] {symbol} 取消后 {_verify_max}s 仍有未结订单, "
            f"已取消 {len(_canceled)} 单 (继续尝试)"
        )
    return True, _canceled


# ─────────────────────────────────────────────────────────────────────
# P0-追加 (2026-06-16): broker=0 路径的 coordinator 释放 + DB 同步
# 根因: _close_pre_check 发现 broker 端无持仓时, 仅清理 position_store,
#       未通知 CapitalAllocator 释放资金, 也未写 trade_records,
#       导致 auto_pool 账本累计失真 + 交易记录缺失.
# ─────────────────────────────────────────────────────────────────────

def _release_coordinator_on_broker_zero(scheduler, position) -> None:
    """broker=0 路径: 通知 coordinator 释放资金 (与正常平仓路径 4563-4573 对齐)"""
    try:
        _coord = getattr(scheduler, "coordinator", None)
        if _coord is not None and getattr(_coord, "enabled", False):
            _coord.on_position_closed(
                symbol=position.symbol,
                strategy=getattr(position, "owner_strategy", None),
                position_pct=float(getattr(position, "position_pct", 0.0) or 0.0),
            )
            logger.info(
                f"[平仓·broker0] {position.symbol} coordinator 资金已释放 "
                f"(strategy={getattr(position, 'owner_strategy', '?')}, "
                f"pct={float(getattr(position, 'position_pct', 0.0) or 0.0):.4f})"
            )
    except Exception as _ce:
        logger.debug(f"[平仓·broker0] {position.symbol} coordinator 释放失败 (忽略): {_ce}")


def _sync_trade_store_on_broker_zero(scheduler, position) -> None:
    """broker=0 路径: 同步 trade_store (DB) — 标记仓位已关闭"""
    try:
        _ts = getattr(scheduler, "trade_store", None)
        if _ts:
            # 先标记 closed 状态再 upsert
            _meta = getattr(position, "metadata", {}) or {}
            _meta["_broker_zero_cleanup"] = True
            _meta["_broker_zero_at"] = now_et().isoformat()
            position.metadata = _meta
            _ts.upsert_open_position(
                position, source="broker_zero_cleanup", action="close_broker_zero",
            )
            logger.info(f"[平仓·broker0] {position.symbol} trade_store 已同步 (action=close_broker_zero)")
    except Exception as _tse:
        logger.debug(f"[平仓·broker0] {position.symbol} trade_store 同步失败 (忽略): {_tse}")


def _close_pre_check(scheduler, position) -> Optional[str]:
    """平仓前安全检查: 冷却 / broker 持仓刷新 / 待成交订单清算.

    Returns:
        如果应该跳过, 返回原因字符串 (调用方负责清理 _close_in_progress 并 return);
        None 表示安全, 可继续后续平仓流程.
    """
    # 1. 冷却检查
    _cd_reason = _check_close_cooldown(position.symbol, position.direction)
    if _cd_reason:
        return f"退避冷却: {_cd_reason}"

    # 2. 强制刷新 broker 端持仓
    _broker_qty = _refresh_broker_position_qty(scheduler, position.symbol)
    if _broker_qty is not None and _broker_qty <= 0:
        try:
            _ps = getattr(scheduler, "position_store", None)
            if _ps and _ps.has(position.symbol):
                _ps.remove(position.symbol)
                logger.warning(
                    f"[平仓·broker0持仓] {position.symbol} broker 端已无持仓, "
                    f"内存标记已平仓 (清理 position_store)"
                )
        except Exception as _re:
            logger.debug(f"[平仓·broker0持仓] {position.symbol} 清理 position_store 异常: {_re}")
        # P0-追加 (2026-06-16): 通知 coordinator 释放资金 + DB 同步
        # 根因: 仅清理 position_store 但不清 CapitalAllocator 账本, 导致 auto_pool 持续失真
        _release_coordinator_on_broker_zero(scheduler, position)
        _sync_trade_store_on_broker_zero(scheduler, position)
        _clear_close_retry(position.symbol, position.direction)
        return "broker 端已无持仓 (已标记内存平仓)"

    # 3. 待成交订单清算
    _safe, _canceled = _wait_or_cancel_open_orders(
        scheduler, position.symbol, timeout_sec=60, poll_interval_sec=5,
    )
    if _canceled:
        # 取消旧订单后, 再次刷新一次持仓
        _broker_qty2 = _refresh_broker_position_qty(scheduler, position.symbol)
        if _broker_qty2 is not None and _broker_qty2 <= 0:
            try:
                _ps = getattr(scheduler, "position_store", None)
                if _ps and _ps.has(position.symbol):
                    _ps.remove(position.symbol)
            except Exception:
                pass
            # P0-追加 (2026-06-16): 通知 coordinator 释放资金 + DB 同步
            _release_coordinator_on_broker_zero(scheduler, position)
            _sync_trade_store_on_broker_zero(scheduler, position)
            _clear_close_retry(position.symbol, position.direction)
            return "取消旧订单后 broker 持仓为 0 (已平仓)"

    return None


def manual_unblock(
    scheduler,
    symbol: str,
    direction: Optional[str] = None,
) -> Dict[str, Any]:
    """Dashboard "人工解锁" 后端方法.

    清除 (symbol, direction) 维度的退避状态 + position.metadata 上的
    _close_blocked / _close_in_progress 标记, 允许下一轮 scan 重新尝试平仓.

    Args:
        scheduler: scheduler 实例 (用于访问 position_store)
        symbol: 标的代码 (如 "US.MU")
        direction: "LONG" / "SHORT"; 不传则同时解锁 LONG/SHORT

    Returns:
        {"unblocked": bool, "cleared_keys": [...], "position_unblocked": bool, "reason": str}
    """
    _result: Dict[str, Any] = {
        "unblocked": False, "cleared_keys": [],
        "position_unblocked": False, "reason": "",
    }

    # 1. 清除退避状态

    _dirs = [direction.upper()] if direction else ["LONG", "SHORT"]
    for _d in _dirs:
        _key = (str(symbol), _d)
        if _key in _close_retry_state:
            _close_retry_state.pop(_key, None)
            _result["cleared_keys"].append(_key)
    if _result["cleared_keys"]:
        pass  # 退避状态已清除
    try:
        _ps = getattr(scheduler, "position_store", None)
        if _ps and _ps.has(symbol):
            _pos = _ps.get(symbol)
            if _pos:
                _meta = getattr(_pos, "metadata", {}) or {}
                _was_blocked = bool(_meta.get("_close_blocked"))
                for _k in (
                    "_close_blocked", "_close_blocked_reason", "_close_blocked_at",
                    "_close_in_progress", "_close_in_progress_ts",
                    "_close_fail_count",
                ):
                    _meta.pop(_k, None)
                _pos.metadata = _meta
                _result["position_unblocked"] = _was_blocked
                try:
                    _ts = getattr(scheduler, "trade_store", None)
                    if _ts:
                        _ts.upsert_open_position(
                            _pos, source="manual_unblock", action="manual_unblock",
                        )
                except Exception as _dbe:
                    logger.debug(f"[manual_unblock] DB 同步失败 (忽略): {_dbe}")
    except Exception as _e:
        _result["reason"] = f"position_store 访问异常: {_e}"
        logger.warning(f"[manual_unblock] {symbol} {_e}")

    _result["unblocked"] = bool(_result["cleared_keys"]) or _result["position_unblocked"]
    logger.warning(
        f"[manual_unblock] {symbol} dirs={_dirs} cleared_keys={_result['cleared_keys']} "
        f"position_unblocked={_result['position_unblocked']}"
    )
    return _result


# ══════════════════════════════════════════════════════════════════
#  执行优化: TWAP 拆单 + 延迟/滑点监控 (Task #14, 2026-05-27)
# ══════════════════════════════════════════════════════════════════

def _load_execution_optimization_config() -> Dict[str, Any]:
    """从 unified_config.yaml 读取 TWAP / 延迟跟踪 配置"""
    defaults = {
        "twap_threshold_usd": 5000.0,
        "twap_slices": 4,
        "twap_interval_seconds": 30.0,
        "enable_latency_tracking": True,
        "enable_twap": True,
    }
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        return {
            "twap_threshold_usd": float(cfg.get_dotted("execution.twap_threshold_usd", defaults["twap_threshold_usd"])),
            "twap_slices": int(cfg.get_dotted("execution.twap_slices", defaults["twap_slices"])),
            "twap_interval_seconds": float(cfg.get_dotted("execution.twap_interval_seconds", defaults["twap_interval_seconds"])),
            "enable_latency_tracking": bool(cfg.get_dotted("execution.enable_latency_tracking", defaults["enable_latency_tracking"])),
            "enable_twap": bool(cfg.get_dotted("execution.enable_twap", defaults["enable_twap"])),
        }
    except Exception as e:
        logger.debug(f"[执行优化] 配置加载失败, 使用默认值: {e}")
        return defaults


def _record_execution_metrics(
    symbol: str,
    direction: str,
    signal_price: float,
    fill_price: float,
    signal_time: float,
    fill_time: float,
    twap_result: Optional[TWAPResult] = None,
) -> Dict[str, Any]:
    """计算执行延迟 + 滑点, 返回 metadata dict (供写入 trade_records.metadata_json)

    同时写日志: "Execution latency: {ms}ms for {symbol}"
    """
    latency_ms = int(max(0.0, (fill_time - signal_time)) * 1000)
    slippage_pct = 0.0
    if signal_price and signal_price > 0 and fill_price and fill_price > 0:
        slip = (float(fill_price) - float(signal_price)) / float(signal_price) * 100.0
        # SHORT 方向: 实际价 < 参考价 = 不利 → 反转符号使 "正 = 不利"
        slippage_pct = -slip if str(direction).upper() == "SHORT" else slip

    # 2026-06-10 红队修复: 滑点超阈值告警 (SlippageController 原为死代码, 此处接入)
    _SLIPPAGE_WARN_PCT = 0.5
    _slippage_alert = False
    if abs(slippage_pct) > _SLIPPAGE_WARN_PCT:
        _slippage_alert = True
        logger.warning(
            f"[滑点告警] {symbol} {direction} 滑点 {slippage_pct:+.2f}% "
            f"(信号${signal_price:.2f} → 成交${fill_price:.2f}) 超过阈值 {_SLIPPAGE_WARN_PCT}%"
        )

    logger.info(f"Execution latency: {latency_ms}ms for {symbol}")

    metadata: Dict[str, Any] = {
        "signal_price": round(float(signal_price or 0.0), 4),
        "fill_price": round(float(fill_price or 0.0), 4),
        "slippage_pct": round(float(slippage_pct), 4),
        "slippage_alert": _slippage_alert,
        "execution_latency_ms": latency_ms,
        "signal_time": datetime.fromtimestamp(signal_time).isoformat() if signal_time else None,
        "fill_time": datetime.fromtimestamp(fill_time).isoformat() if fill_time else None,
        "execution_mode": "TWAP" if twap_result else "MARKET",
    }
    if twap_result is not None:
        metadata["twap"] = twap_result.to_dict()
        if twap_result.degraded:
            metadata["execution_mode"] = "TWAP_DEGRADED"
        if twap_result.canceled:
            metadata["execution_mode"] = "TWAP_CANCELED"
    return metadata


def _submit_order_optimized(
    scheduler,
    order: Order,
    signal_price: float,
    signal_time: float,
    direction: Direction,
    signal_id: str = "",
    trace_id: str = "",
) -> Tuple[Order, Dict[str, Any]]:
    """统一下单入口 — 金额 > 阈值 走 TWAP, 否则走市价; 返回 (order, exec_metadata)

    Args:
        order: 已准备好的订单 (quantity/price/direction 均已设置)
        signal_price: 信号生成时参考价
        signal_time: 信号生成 epoch (s)
        direction: 方向
        signal_id/trace_id: 归因

    Returns:
        (submitted_order, metadata) — metadata 含 slippage_pct/execution_latency_ms/twap 详情
    """
    cfg = _load_execution_optimization_config()
    notional = float(order.quantity) * float(order.price or signal_price or 0.0)
    use_twap = (
        cfg["enable_twap"]
        and should_use_twap(notional, cfg["twap_threshold_usd"])
        and scheduler.execution_engine is not None
    )

    if not use_twap:
        # 普通路径
        if scheduler.execution_engine:
            submitted = scheduler.execution_engine.submit_order(order)
            # P1: 部分成交检查 — 非 TWAP 路径也需记录实际成交量
            _actual_filled = float(getattr(submitted, 'filled_quantity', 0) or 0)
            if 0 < _actual_filled < order.quantity:
                logger.warning(
                    f"[部分成交] {order.symbol} 请求 {order.quantity} "
                    f"实际成交 {_actual_filled}"
                )
                order.filled_quantity = _actual_filled
                order.state = OrderState.PARTIALLY_FILLED
        else:
            # 🔴 P0 (2026-06-13): 无执行引擎时, 实盘必须拒绝
            # 2026-07-03: 跟随 AUTO_TRADE_ACCOUNT (TRADE_ENV 仅控制 DB 路由)
            _auto_acc_noengine = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
            if _auto_acc_noengine == "real":
                order.state = OrderState.REJECTED
                order.failure_reason = "实盘模式下无执行引擎, 拒绝下单"
                logger.error(f"[OrderExecutor] 🔴 REJECTED: {order.symbol} — execution_engine 不存在, 实盘拒绝模拟成交")
                submitted = order
            else:
                order.state = OrderState.FILLED
                order.filled_quantity = order.quantity
                order.avg_fill_price = signal_price or order.price
                order.fill_price = order.avg_fill_price
                order.filled_at = now_et()
                submitted = order

        fill_price = float(
            getattr(submitted, "fill_price", 0)
            or getattr(submitted, "avg_fill_price", 0)
            or order.price
            or signal_price
        )
        fill_time = time.time()
        meta = _record_execution_metrics(
            order.symbol, direction.value if hasattr(direction, "value") else str(direction),
            signal_price, fill_price, signal_time, fill_time, twap_result=None,
        )
        return submitted, meta

    # ── TWAP 路径 ──
    logger.info(
        f"[执行优化] {order.symbol} 订单金额 ≈ ${notional:,.0f} > 阈值 "
        f"${cfg['twap_threshold_usd']:,.0f} → 启用 TWAP 拆单 "
        f"({cfg['twap_slices']} 片 / {cfg['twap_interval_seconds']:.0f}s)"
    )

    def _broker_submit(slice_order: Order) -> Order:
        return scheduler.execution_engine.submit_order(slice_order)

    executor = TWAPExecutor(
        broker_submit=_broker_submit,
        slices=cfg["twap_slices"],
        interval_seconds=cfg["twap_interval_seconds"],
        signal_price=signal_price,
        signal_time=signal_time,
    )

    twap_result = executor.execute_sync(
        symbol=order.symbol,
        direction=direction,
        total_qty=int(order.quantity),
        price_limit=order.price if order.order_type == OrderType.LIMIT else None,
        signal_id=signal_id,
        trace_id=trace_id,
    )

    # 将 TWAP 结果回填到 order 对象, 保持与下游一致
    order.state = OrderState.FILLED if twap_result.filled_qty > 0 else OrderState.REJECTED
    order.filled_quantity = float(twap_result.filled_qty)
    order.avg_fill_price = float(twap_result.avg_fill_price)
    try:
        order.fill_price = float(twap_result.avg_fill_price)
    except Exception as _silent_e:
        logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
    order.filled_at = datetime.fromtimestamp(twap_result.finished_at) if twap_result.finished_at else now_et()
    order.sent_at = datetime.fromtimestamp(twap_result.started_at) if twap_result.started_at else now_et()
    # 首个成交片的 futu_order_id 作为代表 (供归因/查询)
    for s in twap_result.slices:
        if s.futu_order_id:
            order.futu_order_id = s.futu_order_id
            break

    fill_time = twap_result.finished_at or time.time()
    meta = _record_execution_metrics(
        order.symbol, direction.value if hasattr(direction, "value") else str(direction),
        signal_price, twap_result.avg_fill_price,
        signal_time, fill_time, twap_result=twap_result,
    )
    return order, meta


# ═══════════════════════════════════════════════════════════════════
#  工具函数: 价格获取
# ═══════════════════════════════════════════════════════════════════

def _get_current_price(scheduler, symbol: str) -> float:
    """获取当前价格（富途 → Yahoo fallback）"""
    # 优先从执行引擎（富途）获取
    if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
        adapter = scheduler.execution_engine.broker_adapter
        if adapter and adapter.is_connected():
            try:
                price = adapter.get_current_price(symbol)
                if price > 0:
                    return price
            except Exception as e:
                logger.debug(f"[价格获取] adapter.get_current_price({symbol}) 失败: {e}")

    # fallback：通过 MarketDataRouter 获取报价
    try:
        quote = scheduler.data_router.get_quote(symbol)
        if quote:
            # 2026-06-25 修复: 优先使用 bid/ask 中间价, 避免 last_traded_price 在快市中滞后
            bid = float(quote.get("bid", 0) or 0)
            ask = float(quote.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                return round((bid + ask) / 2.0, 2)
            # bid/ask 缺失时的降级路径 (通常只在盘前/盘后或数据源异常时触发)
            logger.warning(f"[价格获取] {symbol} bid/ask 不可用, 降级使用 last_price")
            price_val = quote.get("price", 0)
            # Defensive: price field could be a dict in edge cases (Push cache corruption)
            if isinstance(price_val, dict):
                price_val = price_val.get("price", price_val.get("last_price", 0))
            return float(price_val)
    except Exception as e:
        logger.debug(f"[价格获取] data_router.get_quote({symbol}) 失败: {e}")
    return 0.0


# ═══════════════════════════════════════════════════════════════════
#  持仓冲突处理
# ═══════════════════════════════════════════════════════════════════

def _close_position_for_conflict(scheduler, position: Position):
    """
    因持仓方向冲突而平仓。

    P0 修复 (2026-04-17): 当 position_store 与 broker 不同步时,
    自动关闭冲突持仓, 避免下单失败。
    """
    try:
        price = _get_current_price(scheduler, position.symbol)
        if price <= 0:
            logger.error(f"[持仓冲突] {position.symbol} 无法获取价格, 跳过平仓")
            return

        exit_reason = ExitReason.POSITION_CONFLICT
        _close_position(scheduler, position, price, exit_reason)
        logger.info(f"[持仓冲突] {position.symbol} 已平仓, 等待下轮扫描再开新仓")
        # 设置冷却期, 防止立即反向开仓
        scheduler._set_cooldown(position.symbol)
    except Exception as e:
        logger.error(f"[持仓冲突] {position.symbol} 平仓异常: {e}")


def _close_short_for_long(scheduler, symbol: str, qty: float, current_price: float) -> bool:
    """
    自动平仓空头以准备开多头。

    P1 修复 (2026-04-19): 增加作战计划门禁 — 如果作战计划明确禁止 LONG 方向,
    拒绝执行反向操作。
    P1 优化 (2026-04-17): 当 broker 有空头持仓但新信号为 LONG 时,
    自动以市价买入平仓, 避免信号丢失。
    
    Returns:
        True if close was attempted, False if skipped
    """
    from app.core.models import Order as OrderModel, Direction as Dir, OrderType as OT

    # P1 修复: 作战计划门禁 (防止与作战计划背离的反手)
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        # 检查作战计划是否允许此标的做多
        plan_info = mgr.get_target_info(symbol)
        if plan_info:
            plan_dir = plan_info.get("direction", "").upper()
            plan_status = plan_info.get("status", "PRE_MARKET")
            if plan_status == "CANCELLED":
                logger.warning(
                    f"🚫 [作战计划反手门禁] {symbol} 计划已取消, 拒绝平空转多"
                )
                return False
            if plan_dir and plan_dir not in ("LONG", "ANY"):
                logger.warning(
                    f"⚠️ [作战计划反手门禁] {symbol} 计划方向为 {plan_dir}, "
                    f"与反手 LONG 背离, 仅记录告警但不拦截 (允许执行)"
                )
    except Exception as e:
        logger.debug(f"[作战计划反手门禁] 检查异常 (放行): {e}")

    try:
        if current_price <= 0:
            current_price = _get_current_price(scheduler, symbol)
            if current_price <= 0:
                logger.error(f"[空头转多头] {symbol} 无法获取价格, 跳过平仓")
                return False

        # 构建平仓订单 (BUY 平空仓)
        # 2026-06-10: MARKET→LIMIT ± 2% 价格保护
        _close_short_price = round(current_price * (1 + 0.02), 2)
        order = OrderModel(
            client_order_id=f"{symbol}_CLOSE_SHORT_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            symbol=symbol,
            direction=Dir.LONG,  # 买入平空
            order_type=OT.LIMIT,
            quantity=qty,
            price=_close_short_price,
        )
        # P0 修复 (2026-06-03): 走 ExecutionEngine 而非直连 broker_adapter
        # 确保幂等/方向冲突/来源标记全链路生效
        if not hasattr(order, 'metadata') or order.metadata is None:
            order.metadata = {}
        order.metadata['is_exit'] = True
        order.metadata['source_type'] = 'CONFLICT_RESOLUTION'

        if scheduler.execution_engine:
            try:
                result = scheduler.execution_engine.submit_order(order)
                logger.info(
                    f"[空头转多头] {symbol} 空头平仓已提交: "
                    f"qty={qty} price=${current_price:.2f} result={result}"
                )
            except Exception as e:
                logger.error(f"[空头转多头] {symbol} 平仓提交失败: {e}")
                return False

        # 设置冷却期
        scheduler._set_cooldown(symbol)
        logger.info(f"[空头转多头] {symbol} 空头已平仓, 下轮扫描将尝试开多头")

        # 通知 (2026-08-09 风暴消除: 改走 PushHub — 统一去重/聚合通道,
        #   原实现直接 DingTalkNotifier.send_text 绕过 PushHub)
        try:
            from app.push import get_push_hub, PushMessage, Level, Category
            get_push_hub().push(PushMessage(
                title=f"🔄 持仓冲突自动处理 ({symbol})",
                body=(
                    f"📌 标的: {symbol}\n"
                    f"📉 操作: 平空仓 {int(qty)} 股 @ ${current_price:.2f}\n"
                    f"📈 原因: 新信号 LONG, 自动清理反向空头\n"
                    f"⏳ 下轮扫描将尝试开多头"
                ),
                level=Level.HIGH,
                category=Category.SYSTEM,
                dedup_key=f"pos_conflict:close_short:{symbol}",
            ))
        except Exception as e:
            logger.warning(f"[空头转多头] 通知失败: {e}")
        return True

    except Exception as e:
        logger.error(f"[空头转多头] {symbol} 处理异常: {e}")
        return False


def _close_long_for_short(scheduler, symbol: str, qty: float, current_price: float) -> bool:
    """
    自动平仓多头以准备开空头（通过期权）。

    P1 修复 (2026-04-19): 增加作战计划门禁 — 如果作战计划明确禁止 SHORT 方向,
    拒绝执行反向操作。
    P1 优化 (2026-04-17): 当 broker 有多头持仓但新信号为 SHORT 时,
    自动卖出平仓, 然后通过期权做空。
    
    Returns:
        True if close was attempted, False if skipped
    """
    from app.core.models import Order as OrderModel, Direction as Dir, OrderType as OT

    # P1 修复: 作战计划门禁
    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        plan_info = mgr.get_target_info(symbol)
        if plan_info:
            plan_dir = plan_info.get("direction", "").upper()
            plan_status = plan_info.get("status", "PRE_MARKET")
            if plan_status == "CANCELLED":
                logger.warning(
                    f"🚫 [作战计划反手门禁] {symbol} 计划已取消, 拒绝平多转空"
                )
                return False
            if plan_dir and plan_dir not in ("SHORT", "ANY"):
                logger.warning(
                    f"⚠️ [作战计划反手门禁] {symbol} 计划方向为 {plan_dir}, "
                    f"与反手 SHORT 背离, 仅记录告警但不拦截 (允许执行)"
                )
    except Exception as e:
        logger.debug(f"[作战计划反手门禁] 检查异常 (放行): {e}")

    try:
        if current_price <= 0:
            current_price = _get_current_price(scheduler, symbol)
            if current_price <= 0:
                logger.error(f"[多头转空头] {symbol} 无法获取价格, 跳过平仓")
                return False

        # 构建平仓订单 (SELL 平多仓)
        # 2026-06-10: MARKET→LIMIT ± 2% 价格保护
        _close_long_price = round(current_price * (1 - 0.02), 2)
        order = OrderModel(
            client_order_id=f"{symbol}_CLOSE_LONG_{int(time.time())}_{uuid.uuid4().hex[:6]}",
            symbol=symbol,
            direction=Dir.SHORT,  # 卖出平多
            order_type=OT.LIMIT,
            quantity=qty,
            price=_close_long_price,
        )
        # P0 修复 (2026-06-03): 走 ExecutionEngine 而非直连 broker_adapter
        if not hasattr(order, 'metadata') or order.metadata is None:
            order.metadata = {}
        order.metadata['is_exit'] = True
        order.metadata['source_type'] = 'CONFLICT_RESOLUTION'

        if scheduler.execution_engine:
            try:
                result = scheduler.execution_engine.submit_order(order)
                logger.info(
                    f"[多头转空头] {symbol} 多头平仓已提交: "
                    f"qty={qty} price=${current_price:.2f}"
                )
            except Exception as e:
                logger.error(f"[多头转空头] {symbol} 平仓提交失败: {e}")
                return False

        scheduler._set_cooldown(symbol)
        logger.info(f"[多头转空头] {symbol} 多头已平仓, 下轮扫描将尝试开空头(期权)")

        # 通知 (2026-08-09 风暴消除: 改走 PushHub 同上)
        try:
            from app.push import get_push_hub, PushMessage, Level, Category
            get_push_hub().push(PushMessage(
                title=f"🔄 持仓冲突自动处理 ({symbol})",
                body=(
                    f"📌 标的: {symbol}\n"
                    f"📈 操作: 平多仓 {int(qty)} 股 @ ${current_price:.2f}\n"
                    f"📉 原因: 新信号 SHORT, 自动清理反向多头\n"
                    f"⏳ 下轮扫描将尝试期权做空"
                ),
                level=Level.HIGH,
                category=Category.SYSTEM,
                dedup_key=f"pos_conflict:close_long:{symbol}",
            ))
        except Exception as e:
            logger.warning(f"[多头转空头] 通知失败: {e}")
        return True

    except Exception as e:
        logger.error(f"[多头转空头] {symbol} 处理异常: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════
#  通知函数
# ═══════════════════════════════════════════════════════════════════

def _notify_order(scheduler, symbol: str, signal: SignalScore, order: Order, position: Position, voting_result: Dict = None):
    """下单通知 (钉钉 + 飞书)

    P-Push (2026-05-16): 同时走 PushHub (聚合 + 防疲劳) 和旧 notifier.
    yaml `push.enabled=true` 时 PushHub 接管, 旧 notifier 仍发 (双路过渡期).
    收盘后 daily_review 看是否单一通道即可, 然后再删旧路径.

    Task #42 (P0, 2026-05-28): 投票→交易记录数据链路修复
      历史问题: signal_strength/voting_result 写入逻辑被嵌在 ``if scheduler.notifier:``
      块内, 当 notifier 未配置或失败时, position.metadata 永远拿不到投票数据,
      导致平仓时 trade_records 的 signal_strength / final_strength 全为 0.
      修复: 将 position.metadata 写入提到外层, 与 notifier 解耦, 确保投票结果
      始终落到持仓元数据上.
    """
    # ── 双链路并行 (Task #3 2026-06-15): 递增当日自动交易计数 ──
    # 调用达到此处表示订单已成功提交(走主双链路路径)，计入当日自动交易额度。
    try:
        from app.coordinator.dual_mode_params import increment_today_auto_trade_count
        _new_cnt = increment_today_auto_trade_count(1)
        logger.debug(f"[CONSERVATIVE] {symbol} today_auto_trade_count -> {_new_cnt}")
    except Exception as _ic_err:
        logger.debug(f"[CONSERVATIVE] increment counter failed (忽略): {_ic_err}")

    # ── Task #42: 始终先写 position.metadata, 不依赖 notifier 是否启用 ──
    try:
        # P0 修复 (2026-05-27): 推导 entry_reason 并写入 position.metadata
        # 修复 52.2% 交易 entry_reason 为空的归因断链问题
        from app.core.models import derive_entry_reason
        _entry_reason = derive_entry_reason(
            strategy_id=signal.strategy_id,
            source=(signal.metadata or {}).get("source", "") if hasattr(signal, "metadata") else "",
            metadata=signal.metadata if hasattr(signal, "metadata") else {},
        )

        # 信号 + 投票快照 (用于平仓时 trade_records 字段回填)
        # P1 修复 (2026-05-31): 添加 decision_id 到 metadata, 确保平仓时可关联到 signal_snapshots
        _decision_id = (voting_result or {}).get("decision_id", "") or ""
        _signal_snapshot = {
            'signal_strength': int(getattr(signal, 'strength', 0) or 0),
            'signal_confidence': float(getattr(signal, 'confidence', 0.0) or 0.0),
            'strategy_id': getattr(signal, 'strategy_id', '') or '',
            'voting_result': voting_result or {},
            'source_type': 'MAIN_PIPELINE',  # P1 修复：标注来源
            'entry_reason': _entry_reason,   # P0 归因修复
            # ─── P3 (2026-06-02): 入场类型标记 — reversal / trend / panic_breadth ───
            'entry_type': (signal.metadata or {}).get("entry_type", "trend") if hasattr(signal, "metadata") else "trend",
            'signal_id': getattr(signal, 'signal_id', '') or getattr(signal, 'trace_id', '') or '',  # P0 归因
            # P1 修复 (2026-05-31): decision_id 持久化 — 平仓时用于关联 signal_snapshots
            'decision_id': _decision_id,
            # P1 修复 (2026-05-31): decision_reason 归因 — 记录为什么做这笔交易
            'decision_reason': (
                f"strategy={getattr(signal, 'strategy_id', 'unknown')} "
                f"strength={getattr(signal, 'strength', 0)} "
                f"confidence={getattr(signal, 'confidence', 0):.2f} "
                f"regime={getattr(signal, 'market_regime', 'UNKNOWN')}"
            ),
            # Preserve quality scoring metadata
            "quality_factors": signal.metadata.get("quality_factors") if hasattr(signal, "metadata") and signal.metadata else None,
            "fused": signal.metadata.get("fused") if hasattr(signal, "metadata") and signal.metadata else None,
            "fused_count": signal.metadata.get("fused_count") if hasattr(signal, "metadata") and signal.metadata else None,
            "fused_strategies": signal.metadata.get("fused_strategies") if hasattr(signal, "metadata") and signal.metadata else None,
            # ─── P0 归因修复 (2026-06-06): 全链路 trace_id + 因子明细 ───
            # 确保每笔交易从信号生成到平仓全程可追溯
            "trace_id": getattr(signal, 'trace_id', '') or '',
            "factor_detail": (
                (signal.metadata.get("_factor_detail") or signal.metadata.get("factor_detail")
                 or getattr(signal, "factor_detail", None) or getattr(signal, "score_breakdown", None) or {})
                if (hasattr(signal, "metadata") and signal.metadata)
                else (getattr(signal, "factor_detail", None) or getattr(signal, "score_breakdown", None) or {})
            ),
            "signal_tags": signal.metadata.get("signal_tags", "") if (hasattr(signal, "metadata") and signal.metadata) else "",
            # ─── RFC-061 (2026-06-24): A/B cohort 注入 position.metadata ───
            # 供退出链 pre_close.py 读取: cohort B 盈利仓跳过 EOD 持夜到 ~T+5.
            # assign_cohort 确定性 hash, 与 save_trade 同源 (保证 trade_records 与 position.metadata 一致).
            "cohort": assign_cohort(symbol, now_et().strftime("%Y-%m-%d")),
        }

        if hasattr(position, 'metadata') and position.metadata:
            position.metadata.update(_signal_snapshot)
        else:
            position.metadata = dict(_signal_snapshot)
    except Exception as _meta_err:
        # metadata 写入失败不能阻断下单流程, 但要明确告警 (Task #42 要求)
        logger.warning(
            f"[Task#42] {symbol} 写 position.metadata 失败 (signal_strength 链路降级到 attribution/decisions 兜底): {_meta_err}"
        )

    # P-Push (2026-07-17): 成交通知统一单路走 PushHub。
    #   旧 scheduler.notifier.notify_order_filled 已移除 — 它内部同推钉钉+飞书,
    #   与下方 PushHub 重复 (同一笔推 2~3 次)。position.metadata 已在上方 Task#42
    #   解耦写入, 与通知无关, 移除通知不影响数据链路。
    try:
        from app.push import get_push_hub, trade_entry as _tx_entry
        _meta = getattr(position, "metadata", {}) or {}
        _sl = None
        try:
            _isl = getattr(position, "initial_sl", 0) or 0
            _sl = float(_isl) if _isl and float(_isl) > 0 else None
        except Exception:
            _sl = None
        _tp = None
        try:
            _tpl = getattr(position, "take_profit_levels", None) or []
            _tp = [lv["price"] for lv in _tpl if isinstance(lv, dict) and lv.get("price")] or None
        except Exception:
            _tp = None
        _hub_msg = _tx_entry(
            symbol=symbol,
            direction=signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction),
            qty=int(getattr(order, "quantity", 0) or 0),
            price=float(getattr(order, "price", 0) or 0),
            strategy=getattr(position, "owner_strategy", None) or "main",
            trace_id=getattr(signal, "trace_id", "") or getattr(position, "trace_id", ""),
            sl=_sl,
            tp=_tp,
            signal_strength=_meta.get("signal_strength") or None,
            signal_confidence=_meta.get("signal_confidence") or None,
            voting=_meta.get("voting_result") or None,
            source_type=_meta.get("source_type") or "MAIN_PIPELINE",
        )
        get_push_hub().push(_hub_msg)
    except Exception as _pe:
        logger.debug(f"[PushHub·entry] 失败: {_pe}")


def _notify_exit(scheduler, position: Position, reason: ExitReason, pnl: float, pnl_pct: float):
    """平仓通知 — 统一单路走 PushHub (2026-07-17)。

    旧 scheduler.notifier.notify_exit 已移除 (内部同推钉钉+飞书, 与 PushHub 重复)。
    """
    try:
        from app.push import get_push_hub, trade_exit as _tx_exit
        _reason_str = reason.value if hasattr(reason, "value") else str(reason)
        _meta = getattr(position, "metadata", {}) or {}
        _hub_msg = _tx_exit(
            symbol=position.symbol,
            direction=position.direction.value if hasattr(position.direction, "value") else str(position.direction),
            pnl=float(pnl),
            exit_reason=_reason_str,
            strategy=getattr(position, "owner_strategy", None) or "main",
            trace_id=getattr(position, "trace_id", ""),
            signal_strength=_meta.get("signal_strength") or None,
            signal_confidence=_meta.get("signal_confidence") or None,
            source_type=_meta.get("source_type") or "MAIN_PIPELINE",
        )
        get_push_hub().push(_hub_msg)
    except Exception as _pe:
        logger.debug(f"[PushHub·exit] 失败: {_pe}")


# ═══════════════════════════════════════════════════════════════════
#  动态仓位计算
# ═══════════════════════════════════════════════════════════════════

def _get_order_qty(scheduler, symbol: str, atr: float = 0, direction: str = "LONG", context: dict = None) -> int:
    """
    获取订单数量 (基于资本安全水位的动态仓位计算)

    P1-1 修复 (2026-04-17): 使用 _get_current_price 替代 get_full_market_context
    避免重复调用 TwelveData 浪费信用

    Livermore 原则: 根据账户净值、风险承受能力和当前水位动态调整仓位,
    永远不被破产, 永远不陷于绝境。
    """
    DEFAULT_QTY = DEFAULT_ORDER_QTY  # 局部别名，方便引用
    try:
        from app.risk.capital_safety import get_capital_safety_system
        capital = get_capital_safety_system()

        # P0 修复 (2026-05-02): 开仓前实时刷新 buying_power
        # 旧行为: 依赖 position_monitor 每 30s 推送，开仓时 buying_power 可能过时
        # 新行为: 每次计算前刷新 (AccountRefresher 内部 30s 缓存，不增加 API 负担)
        try:
            from app.risk.account_refresher import get_account_refresher
            refresher = get_account_refresher()

            # 注入 broker_adapter (仅首次)
            if refresher._broker is None and scheduler.execution_engine:
                adapter = getattr(scheduler.execution_engine, "broker_adapter", None)
                if adapter and adapter.is_connected():
                    refresher._broker = adapter

            # 刷新账户状态
            if refresher._broker and refresher._broker.is_connected():
                account_info = refresher.refresh()
                if account_info and account_info.get("buying_power", 0) > 0:
                    capital.update_account(
                        current_equity=account_info.get("total_assets", 0),
                        cash=account_info.get("cash", 0),
                        market_value=account_info.get("market_val", 0),
                        buying_power=account_info.get("buying_power", 0),
                    )
                    logger.debug(
                        f"[动态仓位] {symbol} 开仓前账户刷新: "
                        f"equity=${account_info.get('total_assets', 0):,.0f} | "
                        f"buying_power=${account_info.get('buying_power', 0):,.0f}"
                    )
        except Exception as e:
            logger.debug(f"[动态仓位] {symbol} 账户刷新失败 (继续计算): {e}")

        # P1-1 修复: 优先使用 Futu LV2 实时价格
        price = _get_current_price(scheduler, symbol)

        if price <= 0:
            logger.warning(f"[动态仓位] {symbol} 无法获取价格, 拒绝下单 (fail-close)")
            return 0

        # P0 + P1-2 修复 (2026-05-01): ATR 动态止损 + session-aware 入场
        # atr 参数由调用方传入 (来自 market context)，避免重复获取
        stop_loss_price = price * 0.90  # 默认 fallback: 做多 10%
        if atr and atr > 0:
            # P1-2: 使用 session_vol_ratio 使入场/退出止损标准一致
            try:
                from app.core.shared_kernel import get_shared_kernel
                kernel = get_shared_kernel()
                session = kernel.get_current_session()
                session_ratio = kernel.get_session_vol_ratio(session)
            except Exception:
                session_ratio = 1.0  # 降级

            effective_atr = atr * session_ratio
            # F32 修复 (2026-05-12 → 2026-06-14 SSOT): 从 exit_policy.yaml 读取, 不再硬编码
            # BUG FIX: 原硬编码 2.0 与 exit_policy.yaml atr_stop.initial=4.0 不一致
            # 导致入场止损定价 (用于仓位计算) 与退出止损 (ExitMonitor) 脱节
            try:
                from app.risk.exit_policy_guardian import ExitPolicyGuardian as _EPG
                _exit_policy = _EPG()._load_exit_policy_dict()
                atr_mult = float(_exit_policy.get("exit_rules", {}).get(
                    "atr_stop", {}).get("multiplier_by_profit", {}).get("initial", 4.0))
            except Exception:
                atr_mult = 4.0  # exit_policy.yaml atr_stop.initial 默认
            if hasattr(scheduler, 'position_store') and scheduler.position_store.has(symbol):
                atr_mult = atr_mult * 0.9  # 已有持仓 → 收紧 10% (加仓场景, 原比例 1.8/2.0=0.9)

            # 入场方向: 做多止损在下方，做空止损在上方
            if hasattr(direction, 'value'):
                is_short = direction.value == "SHORT"
            elif isinstance(direction, str):
                is_short = direction.upper() == "SHORT"
            else:
                is_short = False
            if is_short:
                stop_loss_price = price + effective_atr * atr_mult
                hard_stop = price * 1.15
                stop_loss_price = min(stop_loss_price, hard_stop)
            else:
                stop_loss_price = price - effective_atr * atr_mult
                hard_stop = price * 0.85
                stop_loss_price = max(stop_loss_price, hard_stop)

            # ── 双链路并行 (Task #3): 谨慎模式止损收紧 ──
            # conservative: factor=0.7 → 止损距离缩窄30% (更早触发, 减少单笔亏损)
            # normal: factor=1.0 → 不调整
            # aggressive: factor=1.2 → 止损距离放宽20% (给更多波动空间)
            try:
                from app.coordinator.dual_mode_params import get_conservative_param
                _sl_factor = get_conservative_param("stop_loss_tighten_factor")
                if _sl_factor and float(_sl_factor) != 1.0:
                    _orig_sl = stop_loss_price
                    _stop_distance = abs(stop_loss_price - price)
                    _new_distance = _stop_distance * float(_sl_factor)
                    if is_short:
                        stop_loss_price = price + _new_distance
                        hard_stop_adj = price * 1.15
                        stop_loss_price = min(stop_loss_price, hard_stop_adj)
                    else:
                        stop_loss_price = price - _new_distance
                        hard_stop_adj = price * 0.85
                        stop_loss_price = max(stop_loss_price, hard_stop_adj)
                    logger.info(
                        f"[CONSERVATIVE] {symbol} SL tightened: ${_orig_sl:.2f} → "
                        f"${stop_loss_price:.2f} (factor={_sl_factor})"
                    )
            except Exception as _sl_err:
                logger.debug(f"[CONSERVATIVE] {symbol} SL tighten skipped: {_sl_err}")

            logger.debug(
                f"[动态仓位] {symbol} ATR=${atr:.2f} session_ratio={session_ratio:.2f} "
                f"effective_atr=${effective_atr:.2f}, 动态止损 ${stop_loss_price:.2f} ({atr_mult}x)"
            )
        else:
            logger.debug(f"[动态仓位] {symbol} ATR 不可用, 使用固定 10% 止损")

        # ── 动态计算仓位 (P3 2026-05-02: 接入 PositionSizer 纯函数 + 资金自适应) ──
        # 2026-05-27: 支持 VOLATILITY_ADJUSTED 方法
        from app.risk.position_sizer import calc_position_size, calc_volatility_adjusted_size, load_config_from_yaml, get_sizing_method
        tiers, water_levels, guards = load_config_from_yaml()
        sizing_method = get_sizing_method()

        if sizing_method == "volatility_adjusted" and atr and atr > 0:
            # 获取 ATR 历史均值 (20天)
            atr_mean = atr  # 默认等于当前值 (scale=1.0)
            try:
                from app.core.config_container import get_config
                _cfg = get_config()
                # 尝试从 market_data 获取历史 ATR 均值
                _atr_mean_from_ctx = (context or {}).get("atr_mean", 0)
                if _atr_mean_from_ctx > 0:
                    atr_mean = _atr_mean_from_ctx
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

            sizing_result = calc_volatility_adjusted_size(
                equity=capital._state.current_equity,
                buying_power=capital._state.buying_power,
                entry_price=price,
                stop_loss_price=stop_loss_price,
                atr_current=atr,
                atr_mean=atr_mean,
                tiers=tiers,
                water_levels=water_levels,
                drawdown_pct=capital._state.drawdown_pct,
                guards=guards,
            )
        else:
            sizing_result = calc_position_size(
                equity=capital._state.current_equity,
                buying_power=capital._state.buying_power,
                entry_price=price,
                stop_loss_price=stop_loss_price,
                tiers=tiers,
                water_levels=water_levels,
                drawdown_pct=capital._state.drawdown_pct,
                guards=guards,
            )

        size = sizing_result.shares
        logger.info(
            f"[动态仓位] {symbol} ${price:.2f} | "
            f"止损 ${stop_loss_price:.2f} | "
            f"仓位={size} 股 (${size*price:,.0f}) | "
            f"tier={sizing_result.tier} | wl={sizing_result.water_level} | "
            f"pct={sizing_result.position_pct*100:.1f}%"
        )

        # Task #13 (2026-05-27): 叠加 regime_adapter 仓位缩放 (HIGH_VOL=50% etc.)
        # 与 VOLATILITY_ADJUSTED 不冲突: ATR scale 描述个股波动, regime scale 描述全市场状态
        # regime 从 shared_kernel / VIX 代理推断 (与上游 pre_trade_check 一致)
        try:
            _regime = None
            try:
                from app.core.shared_kernel import get_shared_kernel
                _kernel = get_shared_kernel()
                _regime = (
                    getattr(_kernel, "current_regime", None)
                    or getattr(_kernel, "market_regime", None)
                )
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            if not _regime:
                # 以 VIX 作为 fallback (与 pre_trade_check 同逻辑)
                try:
                    from app.analysis.market_state import MarketStateChecker
                    _vix_data = MarketStateChecker()._fetch_vix()
                    _vix = _vix_data[0] if isinstance(_vix_data, tuple) else _vix_data
                    if isinstance(_vix, (int, float)) and _vix >= 25:
                        _regime = "HIGH_VOL"
                except Exception as _silent_e:
                    logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            if _regime and size > 0:
                from app.strategy.regime_adapter import apply_regime_position_scale
                _adj_size, _scale, _note = apply_regime_position_scale(size, _regime)
                if _adj_size != size and _scale < 1.0:
                    logger.info(
                        f"[动态仓位·Regime] {symbol} {_note}"
                    )
                    size = _adj_size
        except Exception as _e:
            logger.debug(f"[动态仓位·Regime] 应用缩放失败 (忽略): {_e}")

        # P0 修复 (2026-05-31): 接入 TradingMode 风控倍数
        # 模拟盘 multiplier=1.0, 实盘保守=0.5, 实盘正常=0.8
        try:
            from app.core.trading_mode import TradingMode
            _tm = TradingMode()
            _tm_mult = _tm.get_risk_multiplier()
            if _tm_mult < 1.0 and size > 0:
                _orig_size = size
                size = max(1, int(size * _tm_mult))
                logger.info(
                    f"[动态仓位·TradingMode] {symbol} {_tm.mode_label}: "
                    f"{_orig_size} → {size} 股 (×{_tm_mult})"
                )
        except Exception as _tme:
            logger.debug(f"[动态仓位·TradingMode] 应用风控倍数失败 (忽略): {_tme}")

        # ── 双链路并行 (Task #3 2026-06-15): 谨慎模式仓位缩放 ──
        # 根据 ModeSwitchService 当前 aggression level 应用缩放因子,
        # 并按单笔金额上限再次截断 (max_per_trade_usd)。
        try:
            from app.coordinator.dual_mode_params import get_conservative_param
            scale_factor = get_conservative_param("position_scale_factor")
            if scale_factor and float(scale_factor) != 1.0 and size > 0:
                _orig_size = size
                size = max(1, int(size * float(scale_factor)))
                # 同时检查单笔金额上限
                max_per_trade = get_conservative_param("max_per_trade_usd")
                if max_per_trade and price > 0 and size * price > float(max_per_trade):
                    size = max(1, int(float(max_per_trade) / price))
                if size != _orig_size:
                    logger.info(
                        f"[CONSERVATIVE] {symbol} qty scaled: {_orig_size} -> {size} "
                        f"(factor={scale_factor})"
                    )
        except Exception as _ce:
            logger.warning(f"[CONSERVATIVE] {symbol} Failed to apply scaling: {_ce}")

        # ── Task #19: position_scale_override (决策引擎下发) ──
        # 由 signal_pipeline Fast Lane 组合相关性检查设置 (如 0.5 表示半仓)
        try:
            _override = (context or {}).get("position_scale_override")
            if _override is not None and size > 0:
                _f = float(_override)
                if 0 < _f < 1.0:
                    _orig_size = size
                    size = max(1, int(size * _f))
                    if size != _orig_size:
                        logger.info(
                            f"[Portfolio] {symbol} 仓位缩减: {_orig_size} -> {size} (×{_f:.2f})"
                        )
        except Exception as _oe:
            logger.debug(f"[Portfolio] position_scale_override 应用异常(忽略): {_oe}")

        # ── Task #3 (2026-06-17): IC监控策略健康仓位缩放 ──
        # REDUCED策略仓位×0.5, SUSPENDED策略已在signal_pipeline被拦截(此处兜底)
        try:
            from app.risk.strategy_health_monitor import get_strategy_health_monitor
            _health_monitor = get_strategy_health_monitor()
            _strat_id = (context or {}).get("strategy_id", "") or "unknown"
            _ic_scale = _health_monitor.get_position_scale(_strat_id)
            if _ic_scale < 1.0 and size > 0:
                _orig_size = size
                size = max(1, int(size * _ic_scale))
                if size != _orig_size:
                    logger.info(
                        f"[IC_Monitor] {symbol} 仓位缩放 ×{_ic_scale} "
                        f"(策略{_strat_id}): {_orig_size} → {size}"
                    )
        except Exception as _ic_err:
            logger.debug(f"[IC_Monitor] {symbol} 仓位缩放失败 (忽略): {_ic_err}")

        # W5 修复 (2026-05-02): 仅在 shares==0 时 fallback，warning 仅记录不阻断
        if sizing_result.warnings:
            for w in sizing_result.warnings:
                logger.warning(f"[动态仓位·告警] {symbol}: {w}")

        if size <= 0:
            logger.warning(f"[动态仓位] {symbol} 计算仓位为 0, 拒绝下单 (fail-close)")
            return 0

        # ═══ SSOT (2026-06-06): 统一安全门禁 — 替代散落 inline 检查 ═══
        # 三层检查: 单品种25% + 方向65% + 总敞口80%
        # 此处用真实 size × price，超限则截断
        try:
            from app.risk.pre_trade_gate import pre_trade_safety_check
            _dir = "SHORT" if direction and "SHORT" in str(direction).upper() else "LONG"
            _ps = getattr(scheduler, 'position_store', None)
            _allowed, _max_qty, _reason = pre_trade_safety_check(
                symbol=symbol, direction=_dir, quantity=size, price=price,
                position_store=_ps,
                broker_adapter=getattr(getattr(scheduler, 'execution_engine', None), 'broker_adapter', None),
            )
            if not _allowed:
                if _max_qty <= 0:
                    logger.warning(f"🚫 [安全门禁·真实] {symbol} 完全拒绝: {_reason}")
                    return 0  # 返回 0，调用方检查后终止下单
                if _max_qty < size:
                    logger.warning(
                        f"🚫 [安全门禁·真实] {symbol} 截断: {size}股 → {_max_qty}股 | {_reason}"
                    )
                    size = _max_qty
        except Exception as _gate_err:
            # fail-close: 门禁异常时拒绝下单，不绕过风控
            logger.error(f"🚫 [安全门禁] {symbol} 检查异常 (拒绝): {_gate_err}")
            return 0

        return size

    except Exception as e:
        # fail-close: 仓位计算异常时拒绝下单，不绕过风控
        logger.error(f"🚫 [动态仓位] {symbol} 计算异常: {e}, 拒绝下单 (fail-close)")
        return 0


# ═══════════════════════════════════════════════════════════════════
#  直接做空策略
# ═══════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════
#  核心下单入口 (风控门禁 → 持仓冲突 → 仓位计算 → 止盈止损 → 执行)
# ═══════════════════════════════════════════════════════════════════


def _check_gates_and_conflicts(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any]) -> bool:
    """检查所有风控门禁与持仓冲突。返回 True 如果允许继续下单。"""
    # ═══ 唯一信号质量门禁 (SSOT) ═══
    # 根因修复 (2026-06-05): 合并原 Gate#1(AND逻辑) + Gate#6(LONG) + Gate#7(SHORT) + Gate#11(SHORT BP)
    # 为 ONE 硬地板检查。无 AND/OR 技巧，无 BP bonus 复杂性，无例外路径。
    # 所有信号源 (main/ORB/TECHNICAL/INSIDER/manual) 必须通过此门禁。
    try:
        _sq_str = int(getattr(signal, 'strength', 0) or 0)
        _sq_conf = float(getattr(signal, 'confidence', 0.0) or 0.0)
        _sq_dir = str(getattr(signal, 'direction', '') or '')
        _sq_is_short = 'SHORT' in _sq_dir.upper() or 'SELL' in _sq_dir.upper()
        # 方向特定阈值 (SSOT: config > 硬编码默认)
        _sq_min = 30  # SHORT 默认
        if not _sq_is_short:
            _sq_min = 35  # LONG 默认
        try:
            from app.core.config_container import get_config as _sq_cfg
            _pt = _sq_cfg().get_dotted("risk.pre_trade", {}) or {}
            if _sq_is_short:
                _sq_min = int(_pt.get("short_min_strength", 30))
            else:
                _sq_min = int(_pt.get("long_min_strength", 35))
        except Exception:
            pass
        if _sq_str < _sq_min:
            logger.warning(
                f"🚫 [SignalQuality] {symbol} {_sq_dir} strength={_sq_str} < {_sq_min} (硬拦截)"
            )
            get_rejection_logger().record(symbol,
                f"strength={_sq_str}<{_sq_min}",
                "SIGNAL_QUALITY_FLOOR", strength=_sq_str,
                confidence=_sq_conf, direction=_sq_dir)
            return False
        if _sq_conf <= 0.0:
            logger.warning(
                f"🚫 [SignalQuality] {symbol} {_sq_dir} confidence={_sq_conf:.3f} 无效 (硬拦截)"
            )
            get_rejection_logger().record(symbol,
                f"confidence={_sq_conf:.3f}<=0",
                "SIGNAL_QUALITY_FLOOR", strength=_sq_str,
                confidence=_sq_conf, direction=_sq_dir)
            return False
    except Exception as _sq_err:
        logger.debug(f"[SignalQuality] 检查异常 (不放行): {_sq_err}")
        return False

    # ── LONG entry filter (2026-06-10): LONG requires elevated dual threshold ──
    # SHORT signals pass through (83% WR); LONG must meet BOTH:
    #   signal_strength >= 50  AND  signal_confidence >= 0.6
    # Fail-close: None/0 strength or confidence → rejected.
    _sq_dir = str(getattr(signal, 'direction', '') or '')
    _sq_str = int(getattr(signal, 'strength', 0) or 0)
    _sq_conf = float(getattr(signal, 'confidence', 0.0) or 0.0)
    
    if _sq_dir == "LONG":
        if _sq_str < 50 or _sq_conf < 0.6:
            logger.warning(
                f"[LONG过滤] {symbol} LONG信号不满足门槛: "
                f"strength={_sq_str} (需≥50), confidence={_sq_conf:.2f} (需≥0.60), "
                f"已跳过 (SHORT方向免过滤)"
            )
            get_rejection_logger().record(symbol,
                f"LONG filter: strength={_sq_str}<50 OR confidence={_sq_conf:.2f}<0.60",
                "LONG_FILTER_REJECTED", strength=_sq_str,
                confidence=_sq_conf, direction=_sq_dir)
            return False

    # 🔴 P0 关键修复: 全局风险状态机门禁检查
    # 在一切交易逻辑之前，检查系统是否处于 RISK_OFF 或 HALT 状态
    try:
        from app.us_pipeline.risk_state_machine import RiskStateMachine, RiskAction
        from app.us_pipeline.risk_state_machine import RiskState  # For logging

        sm = RiskStateMachine()
        # Check if OPEN is allowed
        if not sm.is_trade_allowed(RiskAction.OPEN):
            current_state = sm.get_state().value
            logger.warning(f"🚫 [风控拦截] 拒绝交易 {symbol}: 当前系统处于 {current_state} 状态")
            return False
    except Exception as e:
        logger.error(f"⚠️ 风险状态机检查失败 (放行): {e}")

    # P3 优化 2026-04-29: 中概 ADR 集中度限制
    try:
        from app.risk.capital_safety import get_capital_safety_system, CHINA_ADRS
        adr_allowed, adr_reason = get_capital_safety_system().check_adr_limits(
            symbol, scheduler.position_store.all()
        )
        if not adr_allowed:
            logger.warning(f"[ADR限制] {symbol} 被拦截: {adr_reason}")
            from app.pipeline.position_monitor import _notify_risk_blocked
            _notify_risk_blocked(scheduler, symbol, f"ADR集中度: {adr_reason}")
            return False
    except Exception as e:
        logger.debug(f"[ADR限制] 检查异常 (放行): {e}")

    # ── VIX Regime 仓位系数 + 止损调整 ──
    vix_pos_mult = context.get("vix_position_multiplier", 1.0)
    vix_sl_adj = context.get("vix_stop_loss_adjustment", 1.0)
    vix_regime = context.get("vix_regime", "normal")

    if vix_pos_mult != 1.0 or vix_sl_adj != 1.0:
        # 调整信号 metadata 中的仓位建议
        if hasattr(signal, 'metadata') and signal.metadata:
            original_pct = signal.metadata.get("position_pct", 0.10)
            adjusted_pct = round(original_pct * vix_pos_mult, 4)
            signal.metadata["position_pct"] = adjusted_pct
            signal.metadata["vix_regime"] = vix_regime
            signal.metadata["vix_position_multiplier"] = vix_pos_mult
            signal.metadata["vix_stop_loss_adjustment"] = vix_sl_adj

            logger.info(
                "[VIX Regime] %s | regime=%s | 仓位系数=%.2f (%.0f%%→%.0f%%) | 止损调整=%.2f",
                symbol, vix_regime, vix_pos_mult,
                original_pct * 100, adjusted_pct * 100, vix_sl_adj
            )

            # 危机模式清仓拦截
            if vix_regime == "crisis" and vix_pos_mult == 0.0:
                logger.warning(f"🚫 [VIX 危机] {symbol} VIX 危机模式，拒绝开新仓")
                return False

    # Phase 3 新增：性能衰减检查 (Performance Decay)
    # 根据近期策略胜率动态调整信号强度，防止"连败"期间继续重仓
    try:
        from app.utils.performance_decay import get_decay_factor
        from app.core.db_utils import get_main_db

        db_path = str(get_main_db())
        strategy = context.get("strategy", "unknown")
        decay = get_decay_factor(db_path, strategy, lookback_days=5)

        if decay < 1.0:
            original_strength = signal.strength
            signal.strength = max(10, original_strength * decay)
            logger.warning(f"📉 [性能衰减] 策略 {strategy} 近期表现不佳 -> 信号降权 (x{decay})")

            # 如果强度降得太低，直接拦截
            if signal.strength < 30:
                logger.warning(f"🚫 [性能衰减] {symbol} 信号降权后过低 ({signal.strength})，自动拦截")
                return False
    except Exception as e:
        logger.debug(f"[性能衰减] 检查失败 (不影响主流程): {e}")

    # ── Phase US-1: 美股盘前作战计划门禁 ──
    direction_str = "BUY" if signal.direction == "LONG" else "SELL"
    is_short = (direction_str == "SELL")
    # 信号质量已在 SSOT 门禁 (函数顶部) 统一检查, 此处不再重复

    # ── P0 (2026-05-29): HIGH_VOL 硬拦截 ──
    # 数据: HIGH_VOL regime 133笔, 累计亏损 $39,537
    #   LONG+HIGH_VOL: 7.1% WR — 几乎必输
    #   SHORT+HIGH_VOL: 23.4% WR — 仍然大亏
    # pre_trade_check 已有 VIX>=25 拦截, 这里补充 regime 级拦截
    try:
        _hv_regime = str(getattr(signal, 'market_regime', '') or '').upper()
        if _hv_regime == "HIGH_VOL":
            logger.warning(
                f"🚫 [HIGH_VOL门禁] {symbol} 高波动市况禁止开新仓 "
                f"(regime={_hv_regime}, strength={signal.strength})"
            )
            get_rejection_logger().record(symbol,
                f"HIGH_VOL regime blocked",
                "HIGH_VOL_BLOCKED", strength=signal.strength,
                confidence=None, direction=direction_str)
            return False
    except Exception:
        pass


    # ─── P1 (2026-06-02): Weak 状态过滤 — Muyuan 论文借鉴 ───
    # 区分 "健康超卖" (价格回调, 均线仍上行) vs "结构性下跌" (均线死叉, 趋势向下)
    # 结构性下跌时禁止 LONG — 避免接飞刀
    # 条件: price < MA20 < MA50 且 MA20 斜率为负 → 标记 "Weak" → 禁止 LONG
    if not is_short:
        try:
            _w_price = context.get("price", 0)
            _w_ma20 = context.get("ma20", 0)
            _w_ma50 = context.get("ma50", 0)
            _w_slope = context.get("slope_5m", 0)
            # 也检查 MACD 死叉作为辅助确认
            _w_macd = context.get("macd", 0)
            _w_macd_signal = context.get("macd_signal", 0)

            _is_weak = False
            _weak_reasons = []

            # 核心条件: 价格在所有均线下方 (结构性下跌)
            if _w_price > 0 and _w_ma20 > 0 and _w_ma50 > 0:
                if _w_price < _w_ma20 and _w_ma20 < _w_ma50:
                    _is_weak = True
                    _weak_reasons.append(f"price<{_w_ma20:.0f}<{_w_ma50:.0f}")

            # 辅助条件: 短期斜率为负 (趋势仍在下降)
            if _w_slope < -0.3:
                _weak_reasons.append(f"slope={_w_slope:.2f}%")

            # 辅助条件: MACD 死叉且在水下
            if _w_macd < 0 and _w_macd < _w_macd_signal:
                _weak_reasons.append(f"MACD死叉({_w_macd:.2f})")

            # 反转入场 (广度增强) 可以覆盖 Weak 过滤 — 恐慌反弹时均线必然死叉
            _meta = getattr(signal, 'metadata', None) or {}
            _breadth_boosted = _meta.get("breadth_boosted", False)

            if _is_weak and not _breadth_boosted:
                logger.warning(
                    f"🚫 [Weak门禁] {symbol} 结构性下跌禁止做多: "
                    f"{'; '.join(_weak_reasons)}"
                )
                get_rejection_logger().record(symbol,
                    f"Weak structural decline blocks LONG: {'; '.join(_weak_reasons)}",
                    "WEAK_LONG_BLOCKED", strength=signal.strength,
                    confidence=None, direction=direction_str)
                return False
        except Exception:
            pass

    if is_short:
        # 信号质量已在 SSOT 门禁统一检查 (LONG≥35, SHORT≥30)
        # 此处仅保留做空特有检查: 借券成本 + 轧空风险 + 市况适配

        # 幻方强化门禁
        try:
            from app.risk.short_specific_checks import (
                check_borrow_cost_with_conviction,
                check_short_squeeze,
                check_regime_for_short,
            )
            # Principle 1: 借券成本门控 (含高置信度豁免)
            # P0 修复 (2026-04-30): 传 signal.strength 而非 short_effective
            # check_borrow_cost_with_conviction 内部用 raw strength 判断豁免阈值
            ok, reason = check_borrow_cost_with_conviction(symbol, signal.strength)
            if not ok:
                logger.warning(f"🚫 [借券成本] {symbol} {reason}")
                return False
            # Principle 2: 轧空检测
            ok, reason = check_short_squeeze(symbol)
            if not ok:
                logger.warning(f"🚫 [轧空风险] {symbol} {reason}")
                return False
            # Principle 3: 市况适配 (牛市提高门槛)
            regime = context.get("market_regime", "")
            ok, reason = check_regime_for_short(regime, signal.strength)
            if not ok:
                logger.warning(f"🚫 [市况适配] {symbol} {reason}")
                return False
        except ImportError as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

    try:
        from app.us_pipeline.battle_plan_manager import USBattlePlanManager
        mgr = USBattlePlanManager()
        allowed, reason = mgr.check_trade(symbol, direction_str, strength=signal.strength, confidence=signal.confidence)
        if not allowed:
            logger.warning(f"🚫 [US Plan Gate] 拦截交易 {symbol}: {reason}")
            return False
    except Exception as e:
        logger.debug(f"[US Plan Gate] 检查异常 (放行): {e}")

    # Generate a trace_id for this specific trade attempt
    trace_id = str(uuid.uuid4())[:8]
    logger.info(f"[TradeGuard] 开始评估订单 {symbol} | Trace ID: {trace_id}")

    # ── P0 新增: 价格动量门控 (防止逆势下单) ──
    try:
        from app.risk.price_momentum_gate import get_price_momentum_gate
        direction_str_check = signal.direction.value if hasattr(signal.direction, 'value') else str(signal.direction)
        gate = get_price_momentum_gate()
        passed, reason = gate.check(symbol, direction_str_check)
        if not passed:
            logger.warning(f"🚫 [价格动量门控] 拒绝交易 {symbol}: {reason}")
            return False
        else:
            logger.debug(f"[价格动量门控] {symbol} 通过: {reason}")
    except Exception as e:
        logger.debug(f"[价格动量门控] 检查异常 (放行): {e}")

    # ── P0: 交易保护网关检查 ──
    from app.core.trade_guard import TradeGuard, GuardDecision

    # 获取账户净值 (2026-06-10: 实盘 fail-close)
    try:
        from app.risk.capital_safety import get_capital_safety_system
        equity = get_capital_safety_system()._state.current_equity
        if equity <= 0:
            from app.core.trading_mode import TradingMode
            if TradingMode().is_live:
                logger.critical("🚫 [TradeGuard] 实盘模式但 equity ≤ 0 — 拒绝下单 (fail-close)")
                return False
            equity = 100000.0  # 模拟盘 fallback
    except (AttributeError, ImportError):
        from app.core.trading_mode import TradingMode
        if TradingMode().is_live:
            logger.critical("🚫 [TradeGuard] 实盘模式但 capital_safety 不可用 — 拒绝下单 (fail-close)")
            return False
        equity = 100000.0  # 模拟盘 fallback

    guard = TradeGuard({
        "equity": equity,
        "data_dir": os.path.join(os.path.dirname(__file__), "..", "data")
    })

    # 构建上下文 (包含 trace_id 以便全链路追踪)
    guard_context = {
        "trace_id": trace_id,
        "symbol": symbol,
        "direction": "OPEN_LONG" if signal.direction.value == "LONG" else "OPEN_SHORT",
        "signal_strength": signal.strength,
        "volatility": context.get("atr", 0) / max(0.001, context.get("price", 1)),
        "recent_news": [], 
        "is_reversal": False,
    }

    result = guard.evaluate(guard_context)
    decision = result["decision"]
    reason = result["reason"]
    result_trace = result.get("trace_id", trace_id)

    # 拦截逻辑
    if decision == GuardDecision.BLOCK_OPEN_LONG:
        logger.warning(f"[TradeGuard] 禁止开多 {symbol} | {reason.name} | Trace: {result_trace}")
        return False
    if decision == GuardDecision.BLOCK_OPEN_SHORT:
        logger.warning(f"[TradeGuard] 禁止开空 {symbol} | {reason.name} | Trace: {result_trace}")
        return False
    if decision == GuardDecision.DELAY_AND_WAIT:
        logger.info(f"[TradeGuard] 延迟交易 {symbol} | {reason.name} | Trace: {result_trace}")
        return False
    if decision == GuardDecision.ALLOW_CLOSE_ONLY:
        logger.info(f"[TradeGuard] {symbol} 仅允许平仓逻辑 | Trace: {result_trace}")
        if not scheduler.position_store.has(symbol):
            return False

    # 检查 broker 持仓方向冲突
    if (scheduler.execution_engine
            and scheduler.execution_engine.broker_adapter
            and scheduler.execution_engine.broker_adapter.is_connected()):
        try:
            broker_positions = scheduler.execution_engine.broker_adapter.get_positions()
            for bp in broker_positions:
                if bp["symbol"] == symbol:
                    qty = bp.get("qty", 0)
                    if qty < 0 and signal.direction == Direction.LONG:
                        logger.warning(
                            f"[持仓冲突] {symbol} broker 有 {abs(qty):.0f} 股空头, "
                            f"新信号为 LONG, 自动平仓后再开仓"
                        )
                        success = _close_short_for_long(scheduler, symbol, abs(qty), bp.get("current_price", 0))
                        if not success:
                            logger.warning(f"[持仓冲突] {symbol} 空头平仓失败, 同步 broker 持仓状态")
                            scheduler.position_store.remove(symbol)
                        return False  # 平仓后等待下轮再开新仓
                    elif qty > 0 and signal.direction == Direction.SHORT:
                        logger.warning(
                            f"[持仓冲突] {symbol} broker 有 {qty:.0f} 股多头, "
                            f"新信号为 SHORT, 自动平仓后再做空"
                        )
                        success = _close_long_for_short(scheduler, symbol, qty, bp.get("current_price", 0))
                        if not success:
                            logger.warning(f"[持仓冲突] {symbol} 多头平仓失败, 同步 broker 持仓状态")
                            scheduler.position_store.remove(symbol)
                        return False
        except Exception as e:
            logger.warning(f"[持仓冲突] 检查 broker 持仓失败 (继续执行): {e}")

    # 检查 position_store 中是否存在反向持仓
    if scheduler.position_store.has(symbol):
        existing_pos = scheduler.position_store.get(symbol)
        if existing_pos is not None:  # 2026-06-26: 竞态保护 — has()与get()之间持仓可能被移除
            existing_dir = existing_pos.direction
            if existing_dir != signal.direction:
                logger.warning(
                    f"[持仓冲突] {symbol} 已有{existing_dir.value}持仓, "
                    f"新信号为{signal.direction.value}, 需先平仓"
                )
                _close_position_for_conflict(scheduler, existing_pos)
                return False

    # ── P0 修复 (2026-05-02): PRE_CLOSE_EXIT 竞态防护 ──
    # 防止平仓后立即重入 (NVDA #11→#12 竞态: 7秒内同价重复入场)
    # 检查 trade_records 是否有近期的 PRE_CLOSE_EXIT 记录
    try:
        from app.storage.trade_store import TradeStore as _TS
        trade_store = getattr(scheduler, 'trade_store', None)
        if trade_store:
            cutoff = (now_et() - timedelta(minutes=5)).isoformat()
            conn = trade_store._get_conn()
            recent_close = conn.execute(
                "SELECT exit_time, exit_reason FROM trade_records "
                "WHERE symbol = ? AND exit_time > ? ORDER BY exit_time DESC LIMIT 1",
                (symbol, cutoff)
            ).fetchone()
            conn.close()
            if recent_close and recent_close[1] == 'PRE_CLOSE_EXIT':
                logger.warning(
                    f"[竞态防护] {symbol} 5 分钟内有 PRE_CLOSE_EXIT 记录，"
                    f"拒绝重复入场 (最近平仓: {recent_close[0][:19]})"
                )
                return False
    except Exception as e:
        logger.debug(f"[竞态防护] 检查失败 (继续): {e}")

    # Task #15 (2026-05-27): 风控全部通过，输出统一 [RISK] 日志前缀
    try:
        _trace_id = getattr(signal, "trace_id", "") or "NONE"
        logger.info(
            f"[RISK][trace:{_trace_id}] Pre-trade check passed for {symbol} "
            f"(dir={signal.direction.value if hasattr(signal.direction, 'value') else signal.direction}, "
            f"strength={signal.strength}, confidence={signal.confidence:.2f})"
        )
    except Exception as _silent_e:
        logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

    return True

def _real_pyramid_order(scheduler, symbol: str, signal: SignalScore, kwargs: Dict) -> Dict:
    """金字塔加仓的真实下单 — 通过 execution_engine 提交, 而非假装成功 (2026-06-13 P0 fix)"""
    try:
        from app.core.models import Order, OrderType, Direction
        order = Order(
            order_id=f"PYR_{symbol}_{int(time.time())}",
            symbol=symbol,
            direction=signal.direction if hasattr(signal, 'direction') else Direction.LONG,
            order_type=OrderType.MARKET,
            quantity=kwargs.get('quantity', kwargs.get('qty', 0)),
            price=kwargs.get('price', 0),
            metadata={'source': 'pyramid_add_on'},
        )
        if scheduler.execution_engine:
            result = scheduler.execution_engine.submit_order(order)
            return {
                "success": result.state in (OrderState.FILLED, OrderState.SENT),
                "order_id": order.order_id,
            }
        else:
            logger.error(f"[金字塔] 🔴 无 execution_engine, 拒绝加仓 {symbol}")
            return {"success": False, "error": "no_execution_engine"}
    except Exception as e:
        logger.error(f"[金字塔] 加仓失败 {symbol}: {e}")
        return {"success": False, "error": str(e)}


def _handle_pyramid_add(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any]) -> bool:
    """处理 Livermore 金字塔加仓。返回 True 如果执行了加仓。"""
    # ── Livermore 金字塔加仓处理 (P1-1) ──
    if scheduler.position_store.has(symbol) and signal.direction == Direction.LONG:
        try:
            from app.risk.pyramid_engine import get_pyramid_engine
            pyramid = get_pyramid_engine()
            position = scheduler.position_store.get(symbol)
            price = context.get("price", 0)

            # ── SSOT (2026-06-06): 统一安全门禁 — 加仓前检查 ──
            try:
                from app.risk.pre_trade_gate import pre_trade_safety_check
                _add_qty = int(float(context.get("max_position_size", 50.0)))
                _ps = getattr(scheduler, 'position_store', None)
                _ok, _mq, _rsn = pre_trade_safety_check(
                    symbol=symbol, direction="LONG", quantity=_add_qty, price=price,
                    position_store=_ps,
                    broker_adapter=getattr(getattr(scheduler, 'execution_engine', None), 'broker_adapter', None),
                )
                if not _ok and _mq <= 0:
                    logger.warning(f"🚫 [金字塔·安全门禁] {symbol} 加仓拒绝: {_rsn}")
                    return False
            except Exception as _gate_err:
                # fail-close: 门禁异常时拒绝加仓
                logger.error(f"🚫 [金字塔·安全门禁] {symbol} 异常 (拒绝): {_gate_err}")
                return False

            # 检查是否满足加仓条件
            max_total_size = float(context.get("max_position_size", 50.0))
            can_add, add_reason, add_params = pyramid.can_add_position(
                position=position,
                current_price=price,
                max_total_size=max_total_size,
            )

            if can_add:
                # 执行加仓 (更新现有持仓)
                add_result = pyramid.execute_add_on(
                    position=position,
                    add_params=add_params,
                    order_func=lambda **kwargs: _real_pyramid_order(scheduler, symbol, signal, kwargs),
                )

                if add_result["success"]:
                    logger.info(f"[金字塔加仓] {symbol} 加仓完成: {add_reason}")

                    # P1 优化 (2026-04-18): 随机延迟 10-45 秒
                    import random
                    import time as _time
                    delay = random.uniform(10, 45)
                    logger.info(f"[金字塔加仓] 触发随机延迟 {delay:.0f}s 以分散系统性风险 (Beta 风险)")
                    _time.sleep(delay)

                    # 记录加仓事件
                    scheduler.event_store.append(TradeEvent(
                        event_type="pyramid_add_on",
                        signal_id=signal.signal_id,
                        trace_id=signal.trace_id,
                        position_id=position.position_id,
                        data={
                            "symbol": symbol,
                            "level": add_params['level'],
                            "add_size": add_params['add_size'],
                            "add_price": add_params['add_price'],
                            "new_avg_price": add_params['new_avg_price'],
                            "new_sl": add_params['new_sl'],
                        },
                        source="scheduler",
                        severity=AlertLevel.INFO,
                    ))
                    return  # 加仓完成, 不需要创建新持仓
                else:
                    logger.warning(f"[金字塔加仓] {symbol} 加仓执行失败: {add_result.get('reason')}")
            else:
                logger.debug(f"[金字塔加仓] {symbol} 跳过: {add_reason}")
                return  # 不满足加仓条件, 跳过
        except Exception as e:
            logger.warning(f"[金字塔加仓] {symbol} 异常 (继续常规下单): {e}")

    return False

def _load_short_position_ratio() -> float:
    """幻方 Principle 4: 做空非对称仓位比例 (从 ConfigContainer SSOT)"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        ps = cfg.get_dotted("options.short_strategy.stock_short.position_sizing", {})
        return float(ps.get("short_to_long_ratio", 0.60))
    except Exception:
        return 0.60


def _load_short_exit_params() -> Tuple[float, float]:
    """读取做空止损/止盈比例 (从 ConfigContainer SSOT)"""
    try:
        from app.core.config_container import get_config
        cfg = get_config()
        sc = cfg.get_dotted("options.short_strategy.stock_short", {})
        sl = abs(float(sc.get("stop_loss_pct", -0.08)))
        tp = float(sc.get("take_profit_pct", 0.10))
        return sl, tp
    except Exception:
        return 0.08, 0.10


def _execute_short(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any], voting_result: Dict = None) -> None:
    """做空路由: 正股做空 → Put 期权降级 (幻方增强版)

    策略:
    1. 配置 stock_short.enabled=true 且非模拟盘 → 尝试正股做空
    2. 正股做空失败/不支持 → 自动降级到 Put 期权
    3. 模拟盘 → 直接 Put 期权 (现有行为)
    """
    if signal.direction != Direction.SHORT:
        return

    # Task #3 (2026-06-17): 注入 strategy_id 到 context (供 _get_order_qty IC缩放使用)
    if voting_result and "strategy_id" not in context:
        context["strategy_id"] = (
            voting_result.get("strategy_id", "")
            or getattr(signal, "strategy_id", "")
            or ""
        )

    # SHORT 做空路由：仅支持正股做空（期权路径已移除）
    try:
        _execute_stock_short(scheduler, symbol, signal, context, voting_result)
    except Exception as e:
        logger.error(f"[做空] {symbol} 正股做空失败: {e}", exc_info=True)


def _execute_stock_short(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any], voting_result: Dict = None) -> None:
    """执行正股做空 — 幻方原则增强版

    功能:
    - 非对称仓位 (做空 = 做多 × short_to_long_ratio)
    - exit_policy.yaml short_overrides 止损止盈
    - 技术破位阻力位追踪
    - broker_adapter.try_direct_short() 调用
    - Position / Order 创建

    P2 2026-05-01 (G6+G7): Session-aware entry + entry_session in trail_state
    """
    price = round(float(context.get("price", 0)), 2)
    atr = float(context.get("atr", price * 0.02))

    if price <= 0:
        logger.warning(f"[正股做空] {symbol} 价格为 0，取消")
        return

    # ⭐ Pre-Entry Signal Gate (2026-05-19) — SHORT 入场前 LLM 拦截
    try:
        from app.llm.pre_entry_gate import check_gate
        sig_dict = {
            "strength": int(getattr(signal, "strength", 0) or 0),
            "confidence": float(getattr(signal, "confidence", 0) or 0),
            "factor_detail": (signal.metadata or {}).get("_factor_detail", {}) if hasattr(signal, "metadata") else {},
        }
        gate = check_gate(symbol, "SHORT", sig_dict, market_ctx=context)
        if not gate.go:
            logger.warning(
                f"🚫 [PreEntryGate·SHORT] {symbol} 被 LLM 拦截: {gate.reason} "
                f"(confidence={gate.confidence:.2f}, source={gate.source})"
            )
            try:
                get_rejection_logger().record(
                    symbol, gate.reason or "pre_entry_gate_no",
                    "PRE_ENTRY_GATE", strength=getattr(signal, "strength", 0),
                    confidence=getattr(signal, "confidence", 0), direction="SHORT",
                )
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            return
    except Exception as _ge:
        logger.debug(f"[PreEntryGate·SHORT·exception] {symbol} 检查失败 (放行): {_ge}")

    # P2 2026-05-01: 获取当前 session (G6 + G7)
    from app.core.shared_kernel import get_shared_kernel
    kernel = get_shared_kernel()
    entry_session = kernel.get_current_session()
    session_vol_ratio = kernel.get_session_vol_ratio(entry_session)

    # 非对称仓位 (幻方 Principle 4)
    long_qty = _get_order_qty(scheduler, symbol, atr, direction="SHORT", context=context)
    if long_qty <= 0:
        logger.warning(f"[做空] {symbol} 仓位计算为 0 (安全门禁拒绝)，跳过")
        return
    short_ratio = _load_short_position_ratio()
    quantity = max(1, int(long_qty * short_ratio))

    # 做空差异化止损止盈 (幻方 Principle 1-2) + Session-aware (G6)
    sl_pct, tp_pct = _load_short_exit_params()
    if session_vol_ratio != 1.0:
        sl_pct = sl_pct * session_vol_ratio
        tp_pct = tp_pct * session_vol_ratio
        logger.info(
            f"[Session止损·做空] {symbol} session={entry_session} "
            f"vol_ratio={session_vol_ratio:.2f} → "
            f"SL={sl_pct*100:.1f}% TP={tp_pct*100:.1f}%"
        )
    stop_loss = round(price * (1 + sl_pct), 2)
    take_profit = round(price * (1 - tp_pct), 2)

    # 技术破位阻力位 (幻方 Principle 7)
    resistance = float(context.get("resistance_20d", price * 1.05))

    # 调用 Broker 直接做空
    adapter = None
    if scheduler.execution_engine and hasattr(scheduler.execution_engine, "broker_adapter"):
        adapter = scheduler.execution_engine.broker_adapter

    if adapter is None or not adapter.is_connected():
        raise RuntimeError("broker_adapter 不可用")

    result = adapter.try_direct_short(symbol, quantity, price)
    if not result.get("success"):
        raise RuntimeError(f"try_direct_short 失败: {result.get('error')}")

    # 创建 Position
    position = Position(
        strategy_id=signal.strategy_id or "main_pipeline",  # P0 (2026-06-05): 统一fallback
        signal_id=signal.signal_id,
        trace_id=signal.trace_id,
        symbol=symbol,
        direction=Direction.SHORT,
        size=float(quantity),
        avg_entry_price=price,
        initial_sl=stop_loss,
        initial_tp=take_profit,
        current_sl=stop_loss,
        take_profit_levels=[
            {"ratio": 0.4, "price": take_profit},
            {"ratio": 0.6, "price": round(price * (1 - tp_pct * 1.5), 2)},
        ],
        time_stop_hours=int(_load_short_config_val("time_decay.max_holding_days", 5) * 24),
        market_regime=getattr(signal, "market_regime", "") or MarketRegime.RANGE,
    )
    position.entry_time = now_et()
    position.futu_order_id = result.get("futu_order_id", "")
    position.trail_state = {
        "resistance_20d": resistance,
        "lowest_price": price,
        "prev_scan_price": price,
        "triggered_tiers": [],
        "scale_out_original_size": float(quantity),
        "entry_session": entry_session,  # P2 G7: 开仓时写入
    }

    scheduler.position_store.add(symbol, position)
    # R1: 标记仓位创建成功, lock 归 _close_position → on_position_closed 释放
    if isinstance(context, dict):
        context["_r1_position_created"] = True

    # ── P1 (2026-06-16): 开仓后即时 sync (5s debounce) ──
    try:
        from app.core.position_sync import get_us_position_sync
        get_us_position_sync().request_immediate_sync()
    except Exception:
        pass

    # PostTradeHook (2026-05-16): SHORT 也走统一钩子 (替代散乱 mirror try/except)
    try:
        from app.pipeline.post_trade_hook import on_position_opened
        on_position_opened(position, signal, context or {}, scheduler)
    except Exception as _pte:
        logger.debug(f"[PostTradeHook·SHORT] 提交失败 (不影响下单): {_pte}")

    # 创建 Order
    order = Order(
        client_order_id=f"{symbol}_SHORT_{int(time.time())}_{uuid.uuid4().hex[:6]}",
        symbol=symbol,
        direction=Direction.SHORT,
        order_type=OrderType.MARKET,
        quantity=quantity,
        price=price,
    )
    order.futu_order_id = result.get("futu_order_id", "")

    # 事件 + 通知
    _notify_order(scheduler, symbol, signal, order, position, voting_result)
    scheduler.event_store.append(TradeEvent(
        event_type="order_submitted",
        signal_id=signal.signal_id,
        order_id=order.order_id,
        data={
            "symbol": symbol, "direction": "SHORT",
            "method": "stock_short", "quantity": quantity,
            "price": price, "stop_loss": stop_loss,
            "take_profit": take_profit, "resistance": resistance,
        },
        source="scheduler",
    ))

    logger.info(
        f"[正股做空] ✅ {symbol} 成功 qty={quantity} price=${price:.2f} "
        f"SL=${stop_loss:.2f} TP=${take_profit:.2f} "
        f"resistance=${resistance:.2f} futu_order_id={order.futu_order_id}"
    )


def _load_short_config_val(dotted_key: str, default: Any = None) -> Any:
    """从 ConfigContainer 读取做空配置"""
    try:
        from app.core.config_container import get_config
        return get_config().get_dotted(f"options.short_strategy.stock_short.{dotted_key}", default)
    except Exception:
        return default


def _execute_long(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any], voting_result: Dict = None) -> None:
    """处理做多路由 (标准开仓)。

    P2 2026-05-01: Session-aware entry — 入场止损 = base_sl × session_vol_ratio,
    entry_session 在开仓时写入 trail_state 供退出监控器路径依赖使用。
    """
    price = round(context.get("price", 0), 2)
    atr = context.get("atr", price * 0.02)

    # Task #3 (2026-06-17): 注入 strategy_id 到 context (供 _get_order_qty IC缩放使用)
    if voting_result and "strategy_id" not in context:
        context["strategy_id"] = (
            voting_result.get("strategy_id", "")
            or getattr(signal, "strategy_id", "")
            or ""
        )

    if price <= 0:
        logger.warning(f"[下单] {symbol} 价格为 0，取消下单")
        return

    # P4 观察 (2026-05-19): 信号强度反向 — 仅打日志, 不改决策.
    # 5/12-18 数据: strength 80+ 共 298 笔 avg -$1; strength 60-79 共 291 笔 avg +$11.
    # 如果今晚高 strength 信号继续亏损, 数据驱动决定是否反转评分.
    _strength_observed = int(getattr(signal, "strength", 0) or 0)
    if _strength_observed >= 80:
        logger.info(
            f"[P4·observation] {symbol} LONG strength={_strength_observed} ≥80, "
            f"5/12-18 同档位 avg=-$1/笔. 观察今晚表现. (此处仅记录, 不改行为)"
        )
        # 2026-06-10 P1-3: 实盘保守模式下 strength≥80 封顶为 70 (数据: 80+ avg -$1 vs 60-79 avg +$11)
        try:
            from app.core.trading_mode import TradingMode
            if TradingMode().is_conservative:
                signal.strength = 70
                logger.info(f"[P1-3] {symbol} strength {_strength_observed}→70 (conservative mode cap)")
        except Exception:
            pass

    # ⭐ Pre-Entry Signal Gate (2026-05-19) — LLM 拦截历史失败模式
    # 默认 disabled (yaml.llm_features.pre_entry_gate.enabled=false), 启用后:
    # - 5/12-18 LCID LONG 14 笔历史 0 胜率 → LLM go=False 拒入场
    # - LLM 失败/超时 → 默认放行 (向后兼容)
    try:
        from app.llm.pre_entry_gate import check_gate
        sig_dict = {
            "strength": int(getattr(signal, "strength", 0) or 0),
            "confidence": float(getattr(signal, "confidence", 0) or 0),
            "factor_detail": (signal.metadata or {}).get("_factor_detail", {}) if hasattr(signal, "metadata") else {},
        }
        gate = check_gate(symbol, "LONG", sig_dict, market_ctx=context)
        if not gate.go:
            logger.warning(
                f"🚫 [PreEntryGate·LONG] {symbol} 被 LLM 拦截: {gate.reason} "
                f"(confidence={gate.confidence:.2f}, source={gate.source})"
            )
            try:
                get_rejection_logger().record(
                    symbol, gate.reason or "pre_entry_gate_no",
                    "PRE_ENTRY_GATE", strength=getattr(signal, "strength", 0),
                    confidence=getattr(signal, "confidence", 0), direction="LONG",
                )
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            return
    except Exception as _ge:
        logger.debug(f"[PreEntryGate·LONG·exception] {symbol} 检查失败 (放行): {_ge}")

    # P2 2026-05-01: 获取当前 session 和波动率比率 (G6 + G7)
    from app.core.shared_kernel import get_shared_kernel
    kernel = get_shared_kernel()
    entry_session = kernel.get_current_session()
    session_vol_ratio = kernel.get_session_vol_ratio(entry_session)

    # 仓位计算：固定 50 股（可在 watchlist_us.json 每只股票配置 order_qty 覆盖）
    quantity = _get_order_qty(scheduler, symbol, atr, direction="LONG", context=context)
    if quantity <= 0:
        logger.warning(f"[下单] {symbol} 数量为 0，跳过")
        return

    # ============================================================
    # Phase 3: Dynamic Stop-Loss & Take-Profit (Regime + Session Aware)
    # ============================================================

    # ── Livermore 止损纪律强化 (P0-2, 2026-04-16) ──
    # P3 优化 2026-04-29: ETF 宽基使用 MA 锚定止损，不用固定百分比
    CORE_ETFS = {"US.SPY", "US.QQQ", "US.DIA", "US.IWM"}
    _is_core_etf = symbol in CORE_ETFS

    if _is_core_etf:
        LIVERMORE_HARD_STOP_PCT = 0.06  # ETF 最低 6% 止损
        logger.info(f"[ETF宽基] {symbol} 使用 ETF 专用止损参数 (min 6%%)")
    else:
        LIVERMORE_HARD_STOP_PCT = float(context.get("livermore_stop_pct", 0.10))
    LIVERMORE_TRAILING_ACTIVE = True

    # 1. Base Config — 统一配置访问 (P0-3 修复 2026-04-29: 使用 get_dotted 正确的键路径)
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
    except Exception as e:
        logger.debug(f"[动态仓位] 加载配置失败: {e}")
        _cfg = None

    sl_pct     = abs(float(_cfg.get_dotted("risk.stop_loss.default", -0.06))) if _cfg else 0.06
    tp1_pct    = float(_cfg.get_dotted("risk.take_profit.tier1", 0.08)) if _cfg else 0.08
    tp2_pct    = float(_cfg.get_dotted("risk.take_profit.tier2", 0.15)) if _cfg else 0.15
    tp3_pct    = float(_cfg.get_dotted("risk.take_profit.tier3", 0.25)) if _cfg else 0.25
    atr_mult   = float(_cfg.get_dotted("risk.stop_loss.trailing_atr_mult", 2.0)) if _cfg else 2.0

    # 1.5 IntradayScorer 波动率自适应止损 (2026-04-27)
    _scorer_sl = 0
    if voting_result:
        _scorer_sl = voting_result.get("position_suggestion", {}).get("stop_loss_pct", 0)
    if _scorer_sl > 0:
        logger.info(
            f"[Scorer止损] {symbol} scorer建议 {_scorer_sl*100:.1f}% "
            f"(ATR={context.get('atr',0):.2f} VIX={context.get('vix',20):.0f}), 原默认 {sl_pct*100:.0f}%"
        )
        sl_pct = max(sl_pct, _scorer_sl)

    # P2 2026-05-01 (G6): Session-aware 入场止损调整
    # opening_drive (1.50x) → 放宽止损, 容忍开盘噪音
    # midday (0.60x) → 收紧止损, 低波动不需要宽止损
    if session_vol_ratio != 1.0:
        sl_pct_before = sl_pct
        sl_pct = sl_pct * session_vol_ratio
        logger.info(
            f"[Session止损] {symbol} session={entry_session} "
            f"vol_ratio={session_vol_ratio:.2f} → "
            f"止损 {sl_pct_before*100:.1f}% → {sl_pct*100:.1f}%"
        )

    # Livermore 铁律: 止损不得低于硬底线
    if sl_pct < LIVERMORE_HARD_STOP_PCT:
        logger.info(f"[Livermore止损] 原止损 {sl_pct*100:.1f}% < {LIVERMORE_HARD_STOP_PCT*100:.0f}% 底线, 提升至 {LIVERMORE_HARD_STOP_PCT*100:.0f}%")
        sl_pct = LIVERMORE_HARD_STOP_PCT

    # 2. Dynamic Adjustment based on Market Regime
    _regime_vix = float(context.get("vix", 20.0))
    _regime = signal.market_regime

    if _regime == MarketRegime.HIGH_VOL or _regime_vix > 25:
        logger.info(f"[风控] {symbol} 检测到高波动 (VIX={_regime_vix}), 放宽风控参数")
        sl_pct = max(sl_pct, 0.12)
        tp1_pct = 0.15
        tp2_pct = 0.25
        tp3_pct = 0.40
        atr_mult = 3.0

    elif _regime == MarketRegime.TREND_UP:
        logger.info(f"[风控] {symbol} 检测到上涨趋势, 启用趋势跟随参数")
        sl_pct = max(sl_pct, 0.08)
        tp1_pct = 0.12
        tp2_pct = 0.20
        tp3_pct = 0.30
        atr_mult = 2.5

    elif _regime == MarketRegime.RANGE:
        logger.info(f"[风控] {symbol} 检测到震荡市场, 启用快进快出参数")
        sl_pct = max(LIVERMORE_HARD_STOP_PCT, 0.05)
        tp1_pct = 0.06
        tp2_pct = 0.10
        tp3_pct = 0.15
        atr_mult = 1.5

    # ─── P1 (2026-06-02): 反转入场风控放宽 — Muyuan 论文借鉴 ───
    # 论文核心: 不对称止损 (SL 比 TP 更宽), 给反转留发展时间
    # 触发条件: 广度增强 OR RSI<30 (超卖反弹)
    _is_reversal_entry = False
    _reversal_type = "trend"  # P3: 默认 trend, 反转时覆盖
    _meta = getattr(signal, 'metadata', None) or {}
    if _meta.get("breadth_boosted"):
        _is_reversal_entry = True
        _reversal_type = f"panic_breadth_{_meta.get('breadth_tier', 'elevated')}"
        # 广度反转: SL 放宽到 8%, TP 适度提高到 10%/15%/25%
        sl_pct = max(sl_pct, 0.08)
        tp1_pct = max(tp1_pct, 0.10)
        tp2_pct = max(tp2_pct, 0.15)
        tp3_pct = max(tp3_pct, 0.25)
        atr_mult = max(atr_mult, 2.5)
        logger.info(
            f"[反转·广度] {symbol} 反转入场风控放宽: "
            f"SL={sl_pct*100:.1f}% TP={tp1_pct*100:.0f}/{tp2_pct*100:.0f}/{tp3_pct*100:.0f}% "
            f"ATR×{atr_mult:.1f} | 广度层级={_meta.get('breadth_tier', '?')}"
        )
    elif context.get("rsi", 50) < 30 and context.get("ma20", 0) > 0:
        _price = context.get("price", 0)
        _ma20 = context.get("ma20", 0)
        if _ma20 > 0 and _price > 0 and _price < _ma20:
            _is_reversal_entry = True
            _reversal_type = "rsi_oversold_reversal"
            # RSI 超卖反转: SL 放宽到 6%, TP 适度提高
            sl_pct = max(sl_pct, 0.06)
            tp1_pct = max(tp1_pct, 0.08)
            tp2_pct = max(tp2_pct, 0.12)
            tp3_pct = max(tp3_pct, 0.20)
            logger.info(
                f"[反转·RSI超卖] {symbol} RSI={context.get('rsi', 0):.0f}<30, "
                f"反转入场: SL={sl_pct*100:.1f}% TP={tp1_pct*100:.0f}/{tp2_pct*100:.0f}/{tp3_pct*100:.0f}%"
            )
    if _is_reversal_entry:
        signal.metadata["entry_type"] = _reversal_type

        # ─── P2 (2026-06-02): K15 反转确认 — Muyuan 论文借鉴 ───
        # 1-min 超卖信号噪声大, 需 K15 级别确认后再入场
        # 检查: K15 最近 2 根 K 线是否仍在下跌 (close < open)
        # 如果 K15 仍在下跌 → 延迟入场 (不进场)
        _k15_confirmed = True  # 默认允许 (降级运行)
        try:
            _futu = getattr(scheduler, 'data_router', None)
            if _futu and hasattr(_futu, 'futu') and _futu.futu:
                _k15_bars = _futu.futu.get_kline(symbol, ktype="K_15M", count=5)
                if _k15_bars and len(_k15_bars) >= 3:
                    # 检查最近 2 根已完成的 K15 (跳过当前未完成的)
                    _completed = _k15_bars[-3:-1]  # 倒数第3和第2根 (已完成的)
                    _red_count = 0
                    for _bar in _completed:
                        _close = float(_bar.get('close', 0))
                        _open = float(_bar.get('open', 0))
                        if _close < _open:
                            _red_count += 1

                    if _red_count >= 2:
                        _k15_confirmed = False
                        logger.warning(
                            f"🚫 [K15确认] {symbol} K15 仍在下跌 "
                            f"({_red_count}/2 根阴线), 延迟反转入场"
                        )
                    else:
                        _current_bar = _k15_bars[-1]
                        _cur_close = float(_current_bar.get('close', 0))
                        _cur_open = float(_current_bar.get('open', 0))
                        _is_green = _cur_close > _cur_open
                        logger.info(
                            f"[K15确认] {symbol} K15 反转确认: "
                            f"阴线={_red_count}/2, 当前={'阳线' if _is_green else '阴线'} ✓"
                        )
        except Exception as _k15e:
            logger.debug(f"[K15确认] {symbol} 检查异常 (放行): {_k15e}")

        if not _k15_confirmed:
            # K15 未确认 → 不入场
            try:
                get_rejection_logger().record(symbol,
                    "K15 reversal not confirmed (still declining)",
                    "K15_REVERSAL_BLOCKED", strength=signal.strength,
                    confidence=signal.confidence, direction="LONG")
            except Exception:
                pass
            return

    # LLM 策略自定义止盈止损 (优先级高于 regime 默认值)
    if signal.metadata and signal.metadata.get("custom_take_profit_pct"):
        custom_tp = signal.metadata["custom_take_profit_pct"]
        custom_sl = signal.metadata.get("custom_stop_loss_pct", custom_tp / 2)
        logger.info(f"[LLM策略] 使用自定义风控: 止盈+{custom_tp*100:.0f}% 止损-{custom_sl*100:.0f}%")

        if signal.direction == Direction.LONG:
            stop_loss = price * (1 - custom_sl)
            take_profit_1 = price * (1 + custom_tp * 0.6)
            take_profit_2 = price * (1 + custom_tp * 0.8)
            take_profit_3 = price * (1 + custom_tp)
        else:
            stop_loss = price * (1 + custom_sl)
            take_profit_1 = price * (1 - custom_tp * 0.6)
            take_profit_2 = price * (1 - custom_tp * 0.8)
            take_profit_3 = price * (1 - custom_tp)
    else:
        # 默认 regime-based 止盈止损
        if signal.direction == Direction.LONG:
            stop_loss = price - max(atr * atr_mult, price * sl_pct)
            take_profit_1 = price * (1 + tp1_pct)
            take_profit_2 = price * (1 + tp2_pct)
            take_profit_3 = price * (1 + tp3_pct)
        else:
            stop_loss = price + max(atr * atr_mult, price * sl_pct)
            take_profit_1 = price * (1 - tp1_pct)
            take_profit_2 = price * (1 - tp2_pct)
            take_profit_3 = price * (1 - tp3_pct)

    # 创建持仓记录（本地）
    position = Position(
        strategy_id=getattr(signal, 'strategy_id', '') or "main_pipeline",  # P0 (2026-06-05): 保留信号真实来源，禁止硬编码
        signal_id=signal.signal_id,
        trace_id=signal.trace_id,
        symbol=symbol,
        direction=signal.direction,
        size=float(quantity),
        avg_entry_price=price,
        initial_sl=stop_loss,
        initial_tp=take_profit_1,
        current_sl=stop_loss,
        take_profit_levels=[
            {"ratio": 0.30, "price": take_profit_1},
            {"ratio": 0.30, "price": take_profit_2},
            {"ratio": 0.40, "price": take_profit_3},
        ],
        time_stop_hours=int(os.getenv("TIME_STOP_HOURS", "168")),
        market_regime=signal.market_regime,
        trail_state={
            "active": LIVERMORE_TRAILING_ACTIVE,
            "livermore_mode": True,
            "highest_price": price,
            "lowest_price": price,
            "peak_price": price,
            "prev_scan_price": price,
            "initial_atr": atr,
            "atr_mult": atr_mult,
            "entry_session": entry_session,  # P2 G7: 开仓时写入, 供退出监控器路径依赖
        },
    )

    # 构建订单
    order = Order(
        client_order_id=f"{symbol}_{int(time.time())}_{uuid.uuid4().hex[:6]}",
        signal_id=signal.signal_id,
        trace_id=signal.trace_id,
        symbol=symbol,
        direction=signal.direction,
        order_type=OrderType.LIMIT,
        quantity=float(quantity),
        price=price,
        stop_price=stop_loss,
    )

    # P1-5 修复 2026-04-26: 统一在下单前写入归因基线
    # P0 修复 (2026-05-07): trade_date 使用 ET 日期，date('now') 是 UTC 日期
    try:
        import sqlite3 as _sql3
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from app.core.paths import get_trade_records_db
        _et_now = _dt.now(_tz(_td(hours=-4))).strftime("%Y-%m-%d")
        _db = str(get_trade_records_db())
        _conn = _sql3.connect(_db)
        _cur = _conn.cursor()
        # Phase A (2026-05-14): trace 入场快照 + owner_strategy + direction
        _direction_str = getattr(signal.direction, "value", str(getattr(signal, "direction", "LONG")))
        _owner = (signal.metadata or {}).get("owner_strategy") if hasattr(signal, "metadata") else None
        _owner = _owner or str(getattr(signal, 'strategy_id', '') or '') or "main"
        _cur.execute('''
            INSERT OR REPLACE INTO trade_attribution
            (trace_id, trade_date, symbol, direction, signal_strength, signal_confidence,
             owner_strategy, entry_time, entry_price, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (
            getattr(signal, 'trace_id', ''),
            _et_now,
            symbol,
            _direction_str,
            getattr(signal, 'strength', 0),
            getattr(signal, 'confidence', 0.0),
            _owner,
            now_et().isoformat(),
            float(price) if price else 0.0,
        ))
        _conn.commit()
        _conn.close()
    except Exception as _attr_err:
        logger.debug(f"[归因] trade_attribution 写入失败 (不影响下单): {_attr_err}")

    try:
        if scheduler.execution_engine:
            # Task #14 (2026-05-27): 金额 > 阈值 走 TWAP 拆单, 同时记录延迟/滑点
            _signal_time = float(getattr(signal, "created_at_ts", 0) or 0) or time.time()
            try:
                _ca = getattr(signal, "created_at", None)
                if _ca and hasattr(_ca, "timestamp"):
                    _signal_time = _ca.timestamp()
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            submitted_order, _exec_meta = _submit_order_optimized(
                scheduler, order, signal_price=price, signal_time=_signal_time,
                direction=signal.direction, signal_id=signal.signal_id, trace_id=signal.trace_id,
            )
            # 将执行元数据挂到 position.metadata, 供平仓时写入 trade_records.metadata_json
            try:
                if not hasattr(position, "metadata") or position.metadata is None:
                    position.metadata = {}
                position.metadata["execution"] = _exec_meta
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            # TWAP 下实际成交均价可能偏离信号价, 以成交均价作为持仓入场价
            try:
                _avg = float(getattr(submitted_order, "avg_fill_price", 0) or getattr(submitted_order, "fill_price", 0) or 0)
                if _avg > 0 and abs(_avg - price) / max(price, 1e-6) > 1e-6:
                    position.avg_entry_price = _avg
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            logger.info(
                f"[EXEC][trace:{getattr(signal, 'trace_id', '') or 'NONE'}] Order placed for {symbol} "
                f"({signal.direction.value} qty={quantity} price=${price:.2f} "
                f"SL=${stop_loss:.2f} TP1=${take_profit_1:.2f} "
                f"mode={_exec_meta.get('execution_mode')} "
                f"slip={_exec_meta.get('slippage_pct'):+.3f}% latency={_exec_meta.get('execution_latency_ms')}ms)"
            )
        else:
            # 无执行引擎：直接模拟成交
            order.state = OrderState.FILLED
            order.filled_quantity = order.quantity
            order.avg_fill_price = price
            submitted_order = order
            logger.info(
                f"[EXEC][trace:{getattr(signal, 'trace_id', '') or 'NONE'}] Simulated fill for {symbol} "
                f"({signal.direction.value} qty={quantity} price=${price:.2f})"
            )

        # 记录持仓
        submitted_futu_id = getattr(submitted_order, "futu_order_id", "")
        if submitted_futu_id:
            position.futu_order_id = submitted_futu_id
        # Phase A (2026-05-14): 标 owner_strategy + position_pct (供 coordinator 释放资金用)
        try:
            _meta = (signal.metadata or {}) if hasattr(signal, "metadata") else {}
            owner = _meta.get("owner_strategy")
            # ORB fix: fallback to strategy_id when metadata doesn't set owner_strategy
            # Prevents all pure strategies from defaulting to "main"
            _sid = str(getattr(signal, 'strategy_id', '') or '')
            position.owner_strategy = owner or getattr(position, "owner_strategy", None) or _sid or "main"
            # position_pct 优先级: signal.position_pct > signal.metadata.position_pct > 估算
            _pct = float(getattr(signal, "position_pct", 0) or 0)
            if _pct <= 0:
                _pct = float(_meta.get("position_pct", 0) or 0)
            if _pct <= 0 and quantity and price:
                # 兜底: cost / equity (如能拿到 equity)
                try:
                    from app.risk.capital_safety import get_capital_safety_system
                    _eq = get_capital_safety_system()._state.total_equity or 0
                    if _eq > 0:
                        _pct = (quantity * price) / _eq
                except Exception as _silent_e:
                    logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            position.position_pct = round(_pct, 4)
        except Exception:
            position.owner_strategy = "main"
            position.position_pct = 0.0

        # Task #6 (2026-06-15): 根据 owner_strategy 反向推导 PositionSource 源头标记
        try:
            from app.positions import tag_position, PositionSource
            _src_label = getattr(position, "owner_strategy", None) or "main"
            _src_enum = PositionSource.from_str(_src_label)
            if _src_enum == PositionSource.UNKNOWN:
                _src_enum = PositionSource.AUTO_MAIN  # fallback: 主线下单默认主管线
            tag_position(position, _src_enum)
        except Exception as _tag_err:
            logger.debug(f"[order_executor] tag_position 跳过: {_tag_err}")

        # P0-1 (2026-05-14): 原子化 — 先 DB upsert (持久化), 再 in-memory add, 失败回滚.
        # 防 scheduler crash 在 add 与 upsert 之间 → broker 已有仓位但本地全丢.
        _db_persisted = False
        try:
            scheduler.trade_store.upsert_open_position(position, action="open")
            _db_persisted = True
        except Exception as e:
            logger.error(f"[下单·P0-1] DB upsert 失败, 撤销 in-memory add: {e}")
            # 不写 in-memory, 让 broker_reconcile 在下个周期发现 orphan 自行修
            # 关键告警 — 已下单但本地无记录
            try:
                from app.utils.critical_alerts import send_critical
                send_critical(
                    f"⚠️ 持仓 DB 持久化失败 (订单已成交但本地不知道)\n"
                    f"Symbol: {symbol} LONG\n"
                    f"Qty: {quantity} Price: ${price:.2f}\n"
                    f"Error: {e}\n"
                    f"broker_reconcile 将在 5min 内尝试修复",
                    async_mode=True,
                )
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
            return  # 中断后续 in-memory 操作

        # DB 持久化成功后, 写 in-memory PositionStore
        scheduler.position_store.add(symbol, position)
        # R1: 标记仓位创建成功
        if isinstance(context, dict):
            context["_r1_position_created"] = True

        # ── P1 (2026-06-16): 开仓后即时 sync (5s debounce) ──
        try:
            from app.core.position_sync import get_us_position_sync
            get_us_position_sync().request_immediate_sync()
        except Exception:
            pass

        # PostTradeHook (2026-05-16): 4 个副作用异步统一执行 (替代 ~75 行散乱 try/except)
        # 顺序: attribution → mirror → push → thesis_inferer
        # 主线 0 阻塞 (cooldown 30s 远盖过任何同步开销, 但代码结构更清晰)
        try:
            from app.pipeline.post_trade_hook import on_position_opened
            on_position_opened(position, signal, context or {}, scheduler)
        except Exception as _pte:
            logger.debug(f"[PostTradeHook] 提交失败 (不影响下单): {_pte}")

        # Phase A: 通知 coordinator (灰度, 仅当 enabled)
        try:
            _coord = getattr(scheduler, "coordinator", None)
            if _coord is not None and _coord.enabled:
                _coord.record_position_owner(symbol, position.owner_strategy,
                                             float(getattr(signal, "position_pct", 0.0) or 0.0))
        except Exception as _ce:
            logger.debug(f"[Coordinator] record_position_owner 失败: {_ce}")

        # LLM 异步审计 (P1-1 修复 2026-04-29: 盘中禁用)
        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        current_phase = get_market_phase()

        llm_engine = getattr(scheduler, 'llm_engine', None)
        if llm_engine and hasattr(llm_engine, 'audit_position_async'):
            if current_phase == MarketPhase.TRADING:
                try:
                    audit_queue = getattr(scheduler, '_deferred_audit_queue', None)
                    if audit_queue is None:
                        scheduler._deferred_audit_queue = []
                        audit_queue = scheduler._deferred_audit_queue

                    audit_queue.append({
                        "symbol": symbol,
                        "context_keys": list(context.keys())[:10],
                        "price": context.get("price", 0),
                        "direction": signal.direction.value if hasattr(signal, 'direction') else "UNKNOWN",
                        "timestamp": time.time(),
                    })
                    logger.debug(
                        "[LLM 审计] 盘中阶段 → 排队到盘后执行 | %s %s @ $%.2f",
                        symbol, signal.direction.value if hasattr(signal, 'direction') else "?", context.get("price", 0)
                    )
                except Exception as e:
                    logger.debug(f"[LLM 审计] 排队失败 (已忽略): {e}")
            else:
                try:
                    rsshub = getattr(scheduler, 'llm_rsshub', None)
                    llm_engine.audit_position_async(symbol, context, rsshub=rsshub)
                except Exception as e:
                    logger.debug(f"[LLM 审计] 触发失败 (已忽略): {e}")

        # P1 新增: 关联交易结果到决策记录
        try:
            from app.storage.decision_recorder import get_decision_recorder
            recorder = get_decision_recorder()
            decision_id = signal.metadata.get("decision_id") if signal.metadata else None
            if decision_id:
                recorder.link_trade_outcome(
                    decision_id=decision_id,
                    symbol=symbol,
                    direction=signal.direction.value,
                    entry_price=price,
                    quantity=quantity,
                )
                logger.info(f"[DecisionRecorder] 关联交易: {decision_id} -> {symbol} entry=${price:.2f}")
        except Exception as e:
            logger.warning(f"[DecisionRecorder] 关联交易失败 (不影响主流程): {e}")

        # 记录冷却
        scheduler._set_cooldown(symbol)

        # 记录事件
        scheduler.event_store.append(TradeEvent(
            event_type="order_submitted",
            signal_id=signal.signal_id,
            order_id=order.order_id,
            data={
                "symbol": symbol,
                "direction": signal.direction.value,
                "quantity": quantity,
                "price": price,
                "stop_loss": stop_loss,
                "take_profit_1": take_profit_1,
                "strength": signal.strength,
                "confidence": signal.confidence,
            },
            source="scheduler",
            severity=AlertLevel.INFO,
        ))

        # 推送通知
        _notify_order(scheduler, symbol, signal, order, position, voting_result)

        # 自适应轮询：开仓后升级为 P0 (持仓股)
        if scheduler.polling_controller:
            scheduler.polling_controller.update_tier(symbol, StockTier.POSITION)
            logger.info(f"[自适应轮询] {symbol} 开仓 → 升级为 P0 (持仓股)")

    except Exception as e:
        logger.error(f"[下单] {symbol} 失败：{e}", exc_info=True)
        # P0-3 修复 (2026-05-03): 下单失败时清理可能已添加的幽灵仓
        try:
            scheduler.position_store.remove(symbol)
        except KeyError as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
        except Exception as cleanup_err:
            logger.debug(f"[下单] 清理幽灵仓失败: {cleanup_err}")


# ═══════════════════════════════════════════════════════════════════
#  标的级冷却 (P0 2026-05-08)
# ═══════════════════════════════════════════════════════════════════

_symbol_cooldown_cache: Dict[str, float] = {}  # "{symbol}_{direction}" → cooldown_until_timestamp


def _check_symbol_cooldown(symbol: str, direction) -> bool:
    """P0 修复 (2026-05-08): 标的级冷却 — 连续亏损后禁止同方向开仓

    P1 升级 (2026-05-11): 覆盖 ALL 退出原因（不仅是 STRUCTURE_EXIT）
    QCOM 教训: 4 次 DAYTRADE_STOP 亏损 (-8.4% ~ -9.9%) 未被冷却计数
    """
    import time
    from datetime import datetime, timedelta
    try:
        from app.core.config_container import get_config as get_app_config
        cfg = get_app_config()
        sc_cfg = cfg.get_dotted("symbol_cooldown.structure_exit", {})
        consecutive_threshold = sc_cfg.get("consecutive_losses", 3)
        cooldown_hours = sc_cfg.get("cooldown_hours", 24)
    except Exception:
        consecutive_threshold = 3
        cooldown_hours = 24

    cache_key = f"{symbol}_{direction.name if hasattr(direction, 'name') else direction}"
    now = time.time()
    cooldown_until = _symbol_cooldown_cache.get(cache_key, 0)
    if now < cooldown_until:
        remaining = int((cooldown_until - now) / 3600)
        logger.warning(
            f"🚫 [标的冷却] {symbol} {direction} 冷却中，剩余 {remaining}h "
            f"(连续亏损 ≥{consecutive_threshold}次)"
        )
        return False

    # P1 升级 (2026-05-11): 查询最近 24h 同方向 ANY 亏损次数（不限于 STRUCTURE_EXIT）
    try:
        import sqlite3
        from app.core.db_utils import get_main_db
        db_path = get_main_db()
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cutoff = (now_et() - timedelta(hours=24)).strftime("%Y-%m-%d")
            direction_str = direction.name if hasattr(direction, "name") else str(direction)

            rows = conn.execute(
                "SELECT exit_price, pnl_pct, exit_reason FROM trade_records "
                "WHERE symbol = ? AND direction = ? "
                "AND exit_price > 0 AND pnl_pct < 0 "
                "AND trade_date >= ? "
                "ORDER BY id DESC LIMIT ?",
                (symbol, direction_str, cutoff, consecutive_threshold + 5)
            ).fetchall()

        if len(rows) >= consecutive_threshold:
            # 触发冷却
            cooldown_until = now + (cooldown_hours * 3600)
            _symbol_cooldown_cache[cache_key] = cooldown_until
            reasons = [r[2] for r in rows[:3]]
            logger.warning(
                f"🚫 [标的冷却·激活] {symbol} {direction_str} 24h 内亏损 {len(rows)}次 "
                f"(阈值={consecutive_threshold})，冷却 {cooldown_hours}h | 最近原因: {reasons}"
            )
            return False

        # 未达到阈值，放行
        return True

    except Exception as e:
        logger.debug(f"[标的冷却] 查询失败 (放行): {e}")
        return True


def _check_reversal_cooldown(scheduler, symbol: str, direction) -> bool:
    """F1 (2026-05-16): 平仓后反向开仓冷却 — 防 whipsaw

    实证 (5/14-5/15):
    - SNDK SHORT 平仓 90s 内 LONG 开仓 → -$22 亏损
    - CRM  LONG  平仓 173s 内 SHORT 开仓 → -$45 亏损
    - LCID SHORT 平仓 127s 内 LONG 开仓 → -$10 亏损

    机制: 查 trade_records 找最近一笔平仓 (任何 source_type), 若反向且时差 < cooldown 则拒.
    Returns: True = 允许 / False = 冷却中拒绝
    """
    import sqlite3
    from datetime import datetime, timedelta
    try:
        from app.core.config_container import get_config as _gc
        cfg = _gc()
        # 默认 180s (基于实证数据: 3 笔都在 200s 内反向). 沙盒 60s 加速实验.
        cooldown_sec = int(cfg.get_dotted("symbol_cooldown.reversal_seconds", 180))
        # F1: sandbox 可单独覆盖
        sb_override = cfg.get_dotted("mode.sandbox.overrides", {}) or {}
        if cfg.get_dotted("mode.sandbox.enabled", False):
            if "symbol_cooldown.reversal_seconds" in sb_override:
                cooldown_sec = int(sb_override["symbol_cooldown.reversal_seconds"])
    except Exception:
        cooldown_sec = 180

    if cooldown_sec <= 0:
        return True  # 完全禁用

    direction_str = direction.name if hasattr(direction, "name") else str(direction)
    opposite = "SHORT" if direction_str == "LONG" else "LONG"

    try:
        from app.core.db_utils import get_main_db
        db_path = get_main_db()
        with sqlite3.connect(str(db_path)) as conn:
            # 最近一笔反向平仓 (跨 source_type 全量看，因为 whipsaw 可能跨策略)
            cutoff_iso = (now_et() - timedelta(seconds=cooldown_sec)).isoformat()
            row = conn.execute(
                "SELECT direction, exit_time, source_type FROM trade_records "
                "WHERE symbol = ? AND direction = ? AND exit_time IS NOT NULL "
                "AND exit_time > ? "
                "ORDER BY exit_time DESC LIMIT 1",
                (symbol, opposite, cutoff_iso)
            ).fetchone()

        if row:
            _, exit_time_str, src = row
            try:
                exit_dt = datetime.fromisoformat(exit_time_str)
                gap = (now_et() - exit_dt).total_seconds()
                logger.warning(
                    f"🚫 [F1·反向冷却] {symbol} {direction_str} 拒入场: "
                    f"{opposite} 在 {gap:.0f}s 前刚平仓 (源={src}, 冷却={cooldown_sec}s)"
                )
                return False
            except Exception as _silent_e:
                logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
    except Exception as e:
        logger.debug(f"[F1·反向冷却] 查询失败 (放行): {e}")
    return True


# ── P0 (2026-06-02): 系统级 FLASH_EXIT 熔断器 ──
# 数据驱动: 今日 83 笔 FLASH_EXIT 累计 -$14,251 (XLI 24 次, XLK 14 次)
# 机制: 10 分钟内 FLASH_EXIT >= 5 次 → 全局暂停开仓 10 分钟
# 这是标的不相关的系统性问题: 波动率飙升时所有标的同时被闪退
import json as _json_fe

_FLASH_EXIT_TIMESTAMPS: List[float] = []  # 最近 FLASH_EXIT 发生时间
_FLASH_EXIT_WINDOW_SEC = 600   # 10 分钟滑动窗口
_FLASH_EXIT_THRESHOLD = 5       # 窗口内 >= 5 次触发熔断
_FLASH_EXIT_PAUSE_SEC = 600     # 熔断持续 10 分钟
_FLASH_EXIT_PAUSE_UNTIL: float = 0.0  # 全局暂停截止时间
_FLASH_EXIT_STATE_FILE = "/tmp/ftseek_flash_exit_circuit.json"

# 从磁盘恢复 (防重启丢失)
try:
    if os.path.exists(_FLASH_EXIT_STATE_FILE):
        with open(_FLASH_EXIT_STATE_FILE, 'r') as _f:
            _state = _json_fe.load(_f)
            _FLASH_EXIT_TIMESTAMPS.extend(_state.get("timestamps", []))
            _FLASH_EXIT_PAUSE_UNTIL = _state.get("pause_until", 0.0)
        # 只保留窗口内的时间戳
        _cutoff = time.time() - _FLASH_EXIT_WINDOW_SEC
        _FLASH_EXIT_TIMESTAMPS[:] = [t for t in _FLASH_EXIT_TIMESTAMPS if t > _cutoff]
        if _FLASH_EXIT_TIMESTAMPS:
            logger.info(f"[FLASH熔断·恢复] {len(_FLASH_EXIT_TIMESTAMPS)} 次闪退记录, 暂停至 {_FLASH_EXIT_PAUSE_UNTIL:.0f}")
except Exception:
    pass


def _save_flash_exit_state():
    """持久化熔断状态到磁盘"""
    try:
        _cutoff = time.time() - _FLASH_EXIT_WINDOW_SEC
        _active = [t for t in _FLASH_EXIT_TIMESTAMPS if t > _cutoff]
        with open(_FLASH_EXIT_STATE_FILE, 'w') as _f:
            _json_fe.dump({"timestamps": _active, "pause_until": _FLASH_EXIT_PAUSE_UNTIL}, _f)
    except Exception:
        pass


def record_flash_exit():
    """记录一次 FLASH_EXIT (由 position_monitor 调用)"""
    global _FLASH_EXIT_PAUSE_UNTIL
    _now = time.time()
    _FLASH_EXIT_TIMESTAMPS.append(_now)

    # 清理窗口外的旧记录
    _cutoff = _now - _FLASH_EXIT_WINDOW_SEC
    _FLASH_EXIT_TIMESTAMPS[:] = [t for t in _FLASH_EXIT_TIMESTAMPS if t > _cutoff]

    _count = len(_FLASH_EXIT_TIMESTAMPS)
    if _count >= _FLASH_EXIT_THRESHOLD and _now > _FLASH_EXIT_PAUSE_UNTIL:
        _FLASH_EXIT_PAUSE_UNTIL = _now + _FLASH_EXIT_PAUSE_SEC
        logger.critical(
            f"🚨 [FLASH_EXIT 熔断] {_count} 次闪退 / {_FLASH_EXIT_WINDOW_SEC}s "
            f"≥ 阈值 {_FLASH_EXIT_THRESHOLD} → 全局暂停开仓 {_FLASH_EXIT_PAUSE_SEC}s "
            f"(至 {datetime.fromtimestamp(_FLASH_EXIT_PAUSE_UNTIL).strftime('%H:%M:%S')})"
        )
        # 发送钉钉告警
        try:
            from app.notifier.dingtalk import DingTalkNotifier
            DingTalkNotifier().send_text(
                f"🚨 FLASH_EXIT 熔断触发\n"
                f"{_count} 次闪退 / 10min ≥ {_FLASH_EXIT_THRESHOLD}\n"
                f"全局暂停开仓 {_FLASH_EXIT_PAUSE_SEC//60} 分钟\n"
                f"请检查市场波动率和入场信号质量"
            )
        except Exception:
            pass

    _save_flash_exit_state()


def _check_flash_exit_circuit_breaker(symbol: str) -> bool:
    """检查系统级 FLASH_EXIT 熔断器

    Returns: True = 熔断中, 禁止入场 / False = 允许入场
    """
    _now = time.time()
    if _now < _FLASH_EXIT_PAUSE_UNTIL:
        _remaining = int(_FLASH_EXIT_PAUSE_UNTIL - _now)
        logger.warning(
            f"🚫 [FLASH熔断] {symbol} 系统级暂停开仓: 剩余 {_remaining}s "
            f"(近 10min 内 {len(_FLASH_EXIT_TIMESTAMPS)} 次闪退)"
        )
        return True
    return False


# ── P0 (2026-06-02): 标的日亏损限额 — 防死亡螺旋 ──
_symbol_daily_loss_cache: Dict[str, float] = {}  # symbol → cached daily loss (resets on date change)
_symbol_daily_loss_date: str = ""  # 缓存日期

_DAILY_LOSS_LIMIT = 500.0  # 单标的日亏损限额 $500


def _check_symbol_daily_loss(symbol: str) -> bool:
    """检查标的当日累计亏损是否超限

    同一标的当日累计亏损 >= $500 时当日禁入 (防 SNDK 式死亡螺旋:
    30s 内 2 次 flash_exit 累计 -$3,669)

    Returns: True = 允许 / False = 亏损超限禁止入场
    """
    global _symbol_daily_loss_cache, _symbol_daily_loss_date
    from datetime import date as _date
    today = _et_trade_date(now_et())

    # 日期变更时清空缓存
    if today != _symbol_daily_loss_date:
        _symbol_daily_loss_cache.clear()
        _symbol_daily_loss_date = today

    # 优先用缓存 (同一进程内)
    if symbol in _symbol_daily_loss_cache:
        loss = _symbol_daily_loss_cache[symbol]
        if loss <= -_DAILY_LOSS_LIMIT:
            logger.warning(
                f"🚫 [日亏损限额] {symbol} 当日累计亏损 ${loss:.2f} "
                f"已超限 (-${_DAILY_LOSS_LIMIT:.0f})，当日禁入"
            )
            return False
        return True

    # 查询 DB
    try:
        import sqlite3
        from app.core.db_utils import get_main_db
        db_path = get_main_db()
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_records "
                "WHERE symbol = ? AND trade_date = ? AND exit_price > 0",
                (symbol, today)
            ).fetchone()
        daily_pnl = float(row[0]) if row else 0.0
        _symbol_daily_loss_cache[symbol] = daily_pnl
        if daily_pnl <= -_DAILY_LOSS_LIMIT:
            logger.warning(
                f"🚫 [日亏损限额·激活] {symbol} 当日累计亏损 ${daily_pnl:.2f} "
                f"超限 (-${_DAILY_LOSS_LIMIT:.0f})，当日禁入"
            )
            return False
        return True
    except Exception as e:
        logger.debug(f"[日亏损限额] DB 查询失败 (放行): {e}")
        return True


def _update_symbol_daily_loss(symbol: str, pnl: float):
    """更新标的日亏损缓存 (由 position_monitor 在平仓后调用)"""
    global _symbol_daily_loss_cache
    current = _symbol_daily_loss_cache.get(symbol, 0.0)
    _symbol_daily_loss_cache[symbol] = current + pnl


def _submit_order(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any], voting_result: Dict = None):
    """构建订单并通过执行引擎下单 (Refactored 2026-04-29)

    2026-07-02: 冻结观察期统一执行链路 — MAIN_PIPELINE 路径禁止新开仓。
    所有新仓必须走 PLAYBOOK_SIM 路径 (playbook_service._execute_via_dual_broker)。
    此开关确保即使 _skip_execution_in_trading 未覆盖的路径 (如盘前信号) 也无法通过 main 路径下单。
    平仓操作不受此限制 (通过 close_position 路径, 不经过 _submit_order)。
    """
    # ── 硬开关: MAIN_PIPELINE 禁止新开仓 (2026-07-02 冻结期) ──
    _source = (voting_result or {}).get('source_type', '')
    if not _source:
        _meta = getattr(signal, 'metadata', None) or {}
        _source = _meta.get('source_type', '')
    if _source != 'PLAYBOOK_SIM' and _source != 'PLAYBOOK_LIVE' and _source != 'ORB_ENGINE':
        logger.info(f"[链路统一] {symbol} 非playbook路径下单被拦截 (source_type={_source or 'MAIN_PIPELINE'}), "
                    f"冻结期仅允许 PLAYBOOK_SIM 执行新仓")
        return None

    # ── P0 (2026-06-20): pending 防重锁 — 防止 broker_sync 5s 窗口期重复开仓 ──
    # 立即标记 pending, 订单完成/失败后清除. 纯内存操作, 零 IO 开销.
    if hasattr(scheduler, 'position_store'):
        scheduler.position_store.mark_pending(symbol)

    try:
        return _submit_order_inner(scheduler, symbol, signal, context, voting_result)
    finally:
        # 订单完成后清除 pending (无论成功/失败)
        # 成功时: add() 会写入 _positions 并清除 pending
        # 失败时: 这里兜底清除, 防止永久锁定
        if hasattr(scheduler, 'position_store'):
            scheduler.position_store.clear_pending(symbol)


def _submit_order_inner(scheduler, symbol: str, signal: SignalScore, context: Dict[str, Any], voting_result: Dict = None):
    """内部实现: 构建订单并通过执行引擎下单"""

    # ══════════════════════════════════════════════════════════════════════════
    # Task #1 (2026-06-17): 绝对风控底线 — 最先检查, 不受任何 feature flag 控制
    # ══════════════════════════════════════════════════════════════════════════
    # ── P0: AbsoluteCircuitBreaker (Daily/Hourly/Consecutive Loss 硬熔断) ──
    # 此检查必须是第一个, 不受任何开关控制, 是最后防线
    try:
        from app.risk.absolute_circuit_breaker import get_absolute_circuit_breaker
        _acb = get_absolute_circuit_breaker()
        _strategy_id = ""
        if voting_result:
            _strategy_id = voting_result.get("strategy_id", "") or voting_result.get("model", "")
        if not _strategy_id and hasattr(signal, "metadata"):
            _strategy_id = (signal.metadata or {}).get("strategy_id", "")
        _acb_allowed, _acb_reason = _acb.can_open_new_position(_strategy_id)
        if not _acb_allowed:
            logger.warning(
                f"🚨 [AbsoluteCircuitBreaker] {symbol} 新仓被拒: {_acb_reason}"
            )
            return
    except Exception as _acb_err:
        # fail-close: 熔断器检查异常 → 不交易 (安全优先)
        logger.warning(f"🚨 [AbsoluteCircuitBreaker·fail-close] {symbol} 检查异常, 跳过下单: {_acb_err}")
        return

    # 2026-06-25 OKR 架构决策: 主线 pipeline 不切换实盘.
    # Playbook 是指定的唯一 sim→live 毕业策略. 主线仅作为 sim 研究/alpha 来源.
    try:
        import os as _os
        _auto_account = _os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
        if _auto_account == "real":
            from app.core.config_container import get_config as _get_cfg
            # 2026-07-26 扫尾: 原键 main_pipeline.allow_live 不存在恒 False, 真键在 strategy_budgets (yaml 同为 False, 零行为变化)
            _main_allow_live = bool(_get_cfg().get_dotted("strategy_budgets.main_pipeline.allow_live", False))
            if not _main_allow_live:
                logger.warning(
                    f"🚫 [MainPipeline] {symbol} 实盘已被禁用. "
                    f"AUTO_TRADE_ACCOUNT=real 但 strategy_budgets.main_pipeline.allow_live=false. 仅 Playbook 可切换 live."
                )
                return
    except Exception as _mp_err:
        # 2026-07-26 扫尾: 原为异常放行 (fail-open) — 实盘闸门读不到配置时放行是反向安全, 改 fail-closed
        logger.error(f"🚫 [MainPipeline] live 检查异常, fail-closed 拒绝: {_mp_err}")
        if os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower() == "real":
            return

    # ── P0: VIX 数据新鲜度 fail-close (Task #1) ──
    # VIX 数据不可用时拒绝新仓 (宁可错过, 不可放错)
    try:
        from app.analysis.market_state import MarketStateChecker
        _vix_checker = MarketStateChecker()
        _vix_val = _vix_checker._fetch_vix_data()
        if _vix_val is None:
            logger.warning(
                f"🔒 [VIX_FAILCLOSE] {symbol} VIX数据不可用，拒绝新仓"
            )
            return
    except Exception as _vix_err:
        # fail-close: VIX 获取异常 → 不交易
        logger.warning(
            f"🔒 [VIX_FAILCLOSE] {symbol} VIX数据获取异常，拒绝新仓: {_vix_err}"
        )
        return
    # ── P0: VIX>28 硬拒新仓 (Task #2) — 含 Hysteresis 防抖 ──
    try:
        from app.risk.vix_guard import VIXGuard
        from app.core.feature_flags import FeatureFlags
        _vix_guard = VIXGuard()
        _vix_blocked, _vix_block_reason = _vix_guard.should_block(_vix_val)
        if FeatureFlags().is_enabled("vix_hard_blocking") and _vix_blocked:
            logger.warning(f"🚫 [VIX_HARD_BLOCK] {symbol} {_vix_block_reason}")
            return
        elif _vix_blocked:
            logger.info(f"⚠️ [VIX_WARN] {symbol} {_vix_block_reason} (WARN模式，未拦截)")
    except Exception as _vhb_err:
        # VIXGuard 异常不阻塞下单 (fail-open for guard logic only)
        logger.warning(f"⚠️ [VIX_GUARD_ERR] {symbol} VIXGuard异常: {_vhb_err}")
    # ══════════════════════════════ End Task #2 VIX硬拒 ══════════════════════════════

    # ── 自动交易总开关 (Dashboard 单一控制点) ──
    # ModeSwitchService 是唯一的开关来源 (Dashboard toggle 同时写内存+YAML).
    # 关闭时: 信号生成/数据采集不受影响, 仅阻断订单提交
    # 已持仓的退出规则继续生效 (由 position_monitor 处理)
    try:
        from app.web_dashboard.mode_switch_service import get_mode_switch_service
        if not get_mode_switch_service().is_auto_enabled():
            logger.debug(f"🔒 [AUTO] {symbol} 自动交易已关闭 (Dashboard), 跳过下单")
            return
    except Exception as _ae_err:
        # 开关检查异常 → fail-safe: 不交易 (安全优先)
        logger.warning(f"🔒 [AUTO·fail-safe] {symbol} 开关检查异常, 跳过下单: {_ae_err}")
        return


    # ── 账户级熔断 (2026-06-25 OKR): 单日亏损 ≥2% 停止新仓, ≥3% 强制全平 ──
    try:
        from app.risk.account_circuit_breaker import check_circuit_breaker
        from app.risk.capital_safety import get_capital_safety_system
        _cs = get_capital_safety_system()
        _equity = float(getattr(_cs._state, 'current_equity', 0) or 0)
        if _equity > 0:
            _cb = check_circuit_breaker(_equity)
            if _cb.action in ("HALT", "LIQUIDATE"):
                logger.warning(
                    f"🚨 [AccountCircuitBreaker] {symbol} 新仓被拒: {_cb.reason}"
                )
                return
    except Exception as _cb_err:
        logger.warning(f"🚨 [AccountCircuitBreaker·fail-close] {symbol} 检查异常, 跳过下单: {_cb_err}")
        return

    # ── 双链路并行 (Task #3 2026-06-15): 谨慎模式·每日自动交易次数限制 ──
    try:
        from app.coordinator.dual_mode_params import (
            get_conservative_param, get_today_auto_trade_count,
        )
        max_daily = get_conservative_param("max_daily_auto_trades")
        if max_daily:
            today_auto_count = get_today_auto_trade_count()
            if today_auto_count >= int(max_daily):
                logger.info(
                    f"[CONSERVATIVE] {symbol} Daily auto trade limit reached: "
                    f"{today_auto_count}/{max_daily}, skip"
                )
                return
    except Exception as _dl_err:
        logger.warning(f"[CONSERVATIVE] {symbol} daily limit check failed: {_dl_err}")

    # ── 双链路并行 (Task #4): 自动交易预算门禁 ──
    # 检查自动交易敞口是否已达激进度上限 (conservative=30%, normal=60%, aggressive=75%)
    # 同时检查全局 80% 硬约束 (auto + manual 合计)
    try:
        from app.risk.capital_safety import get_capital_safety_system
        _budget = get_capital_safety_system().get_available_budget_for_auto()
        if _budget <= 0:
            logger.info(f"[BUDGET] {symbol} Auto budget exhausted, skip order")
            return
    except Exception as _bg_err:
        logger.debug(f"[BUDGET] {symbol} budget check skipped: {_bg_err}")

    # ── P0 (2026-06-14, 修复 2026-06-18): 信号质量门禁 — 按 strategy_id 分别检查 IC ──
    # 根因修复: 旧逻辑用全局 IC 一刀切，一个坏策略 (IC=-0.23) 拖垮全局 → 好策略也被拦截
    # 修复: 按 strategy_id 分别检查，坏策略拦截、好策略放行
    # 性能修复 (2026-06-18): 加模块级缓存，同一轮次 25 symbols 只算一次 SQL
    # P0-2 (2026-06-21): shadow_only 模式 pass-through — 记录不拦截, 让Paper Trade积累数据
    # 原因: OKR Week 1 目标是积累≥30笔交易数据验证IC, 但IC门禁在shadow模式下拦截所有信号
    #       → 零成交 → 零outcome → IC无法改善 → 自我锁死
    # 安全底线: Kill Switch / 仓位限制 / VIX风控 仍然正常拦截 (不受此改动影响)
    # 2026-06-30: IC gate 门槛从 strategy_registry 读取（原硬编码）
    _sig_strategy_id = getattr(signal, "strategy_id", None) or "unknown"
    try:
        from app.core.strategy_registry import get_strategy_config
        _sr_cfg = get_strategy_config(_sig_strategy_id)
        _IC_HARD_BLOCK = float(_sr_cfg["ic_gate"]["hard_block_threshold"])
        _IC_MIN_THRESHOLD = float(_sr_cfg["ic_gate"]["warning_threshold"])
        _IC_MIN_SIGNALS = int(_sr_cfg["ic_gate"]["min_signals"])
    except Exception:
        _IC_MIN_THRESHOLD = 0.05
        _IC_MIN_SIGNALS = 30
        _IC_HARD_BLOCK = -0.15

    # 检查是否shadow_only模式
    _is_shadow = False
    try:
        from app.core.config_container import get_config
        _cfg = get_config()
        _pipeline_cfg = _cfg.get_section("pipeline", {})
        _is_shadow = _pipeline_cfg.get("master_switch") == "shadow_only"
    except Exception:
        pass

    try:
        from app.analysis.signal_ic_tracker import get_ic_summary
        global _ic_summary_cache, _ic_summary_cache_ts
        import time as _time
        _now = _time.monotonic()
        if not _ic_summary_cache or (_now - _ic_summary_cache_ts) > _IC_CACHE_TTL_SEC:
            _ic_summary_cache = get_ic_summary(min_signals=_IC_MIN_SIGNALS, days_back=30)
            _ic_summary_cache_ts = _now
        _ic = _ic_summary_cache
        _ic_status = _ic.get("status", "unknown")
        if _ic_status == "insufficient_data" and not _is_shadow:
            _n = _ic.get("n_signals", 0)
            logger.warning(
                f"🔒 [IC门禁] {symbol} 信号数据不足 ({_n}/{_IC_MIN_SIGNALS}), "
                f"自动交易暂停 (需更多交易日积累数据)"
            )
            return
        
        # shadow_only模式: 记录IC但不拦截, 让信号通过积累数据
        if _is_shadow:
            _ic_p = _ic.get("ic_p_value", "N/A")
            _ic_mean = _ic.get("ic_mean", 0.0)
            logger.info(
                f"📊 [IC门禁·shadow] {symbol} IC={_ic_mean:.3f} p={_ic_p} "
                f"(shadow_only pass-through, 积累数据)"
            )
        else:
            _by_strategy = _ic.get("by_strategy", {}) or {}
            _sig_strategy = getattr(signal, "strategy_id", None) or "unknown"
            
            if _by_strategy and _sig_strategy in _by_strategy:
                _strat_stat = _by_strategy[_sig_strategy]
                _strat_ic = _strat_stat.get("ic", 0.0)
                _strat_n = _strat_stat.get("n", 0)
                _strat_pval = _strat_stat.get("pval", 1.0)
                
                # 2026-06-26: IC门禁分级拦截 — 只硬拦截 IC < -0.10 (反预测)
                # IC在 [-0.10, 0.05) 区间为"弱负但非反预测", 降级为warning放行
                if _strat_ic is not None and _strat_ic < _IC_HARD_BLOCK and _strat_n >= 10:
                    logger.warning(
                        f"🔒 [IC门禁] {symbol} 策略 {_sig_strategy} IC={_strat_ic:.3f} < {_IC_HARD_BLOCK} "
                        f"(n={_strat_n}), 反预测, 硬拦截"
                    )
                    return
                if _strat_ic is not None and _strat_ic < _IC_MIN_THRESHOLD and _strat_ic >= _IC_HARD_BLOCK and _strat_n >= 10:
                    logger.info(
                        f"⚠️ [IC门禁·warning] {symbol} 策略 {_sig_strategy} IC={_strat_ic:.3f} "
                        f"(弱负, 非反预测, 放行积累数据) (n={_strat_n})"
                    )
                if _strat_pval is not None and _strat_pval >= 0.05 and _strat_ic is not None and _strat_ic >= _IC_HARD_BLOCK and _strat_n >= 10:
                    # 2026-06-26: p-value不显著但IC非反预测 → warning放行 (同IC分级逻辑)
                    logger.info(
                        f"⚠️ [IC门禁·显著性·warning] {symbol} 策略 {_sig_strategy} IC={_strat_ic:.3f} 但 "
                        f"p={_strat_pval:.3f}≥0.05 不显著 (n={_strat_n}), 放行积累数据"
                    )
            else:
                _ic_mean = _ic.get("ic_mean", 0.0)
                if _ic_mean is not None and _ic_mean < -0.10:
                    logger.warning(
                        f"🔒 [IC门禁·全局] {symbol} 全局IC={_ic_mean:.3f} (策略 {_sig_strategy} 无独立数据), "
                        f"信号整体无预测能力, 拦截"
                    )
                    return
            
            _sig_dir = (
                getattr(getattr(signal, "direction", None), "value", None)
                or str(getattr(signal, "direction", "") or "")
            )
            _by_dir = _ic.get("by_direction", {}) or {}
            _dir_stat = _by_dir.get(_sig_dir) if isinstance(_by_dir, dict) else None
            if isinstance(_dir_stat, dict):
                _dir_ic = _dir_stat.get("ic")
                _dir_n = int(_dir_stat.get("n", 0) or 0)
                # 2026-06-26: 方向IC分级 — 只在 IC < -0.10 时硬拦截 (同策略IC逻辑)
                if _dir_ic is not None and _dir_ic < _IC_HARD_BLOCK and _dir_n >= 10:
                    logger.warning(
                        f"🔒 [IC门禁·方向] {symbol} 方向 {_sig_dir} 独立IC={_dir_ic:.3f} (n={_dir_n}) "
                        f"反预测, 硬拦截此方向下单"
                    )
                    return
        
        _ic_p = _ic.get("ic_p_value", "N/A")
        logger.debug(f"[IC门禁] {symbol} IC={_ic.get('ic_mean', 0.0):.3f} p={_ic_p} ≥{_IC_MIN_THRESHOLD} ✅")
    except Exception as _ic_err:
        # IC 检查失败 → fail-safe: 不交易 (安全优先)
        logger.warning(f"🔒 [IC门禁·fail-safe] {symbol} IC检查异常, 跳过下单: {_ic_err}")
        return

    # ── P0 (2026-06-21): shadow_only 模式下单拦截 ──
    # IC门禁pass-through让信号通过IC检查, 但shadow_only模式下不真正下单
    # 信号已在signal_pipeline.py L646-648被shadow_signal_logger记录, 积累数据用于IC验证
    # 这里只阻止真正下单, 不影响shadow数据积累
    if _is_shadow:
        logger.info(
            f"📊 [shadow_only] {symbol} 信号通过IC门禁, 但shadow_only模式不真正下单 "
            f"(已记录到shadow_signal_logger供IC验证)"
        )
        return

    # ── P0 (2026-06-14): Kill Switch 统一 — 自动线也必须尊重紧急制动 ──
    # 原问题: 手动线检查 check_kill_switch()，自动线只检查 auto_trading_enabled
    # Kill Switch 是最高优先级，激活后所有交易（自动+手动）必须停止
    try:
        from app.us_pipeline.emergency_interceptor import check_kill_switch
        if check_kill_switch():
            logger.warning(f"🚨 [Kill Switch] {symbol} 紧急制动已激活，自动下单被拦截")
            return
    except Exception as _ks_err:
        # P1.2 (RFC-060): fail-close — 自动线同样 fail-safe, 检查异常即不下单
        # (原 fail-open logger.debug 会静默绕过紧急制动)
        logger.warning(f"🚨 [Kill Switch] {symbol} 检查异常, fail-safe 不下单: {_ks_err}")
        return

    # ── P0 (2026-06-14): 持仓去重 — 已有仓位直接跳过 ──
    # 原则: 该标的已有持仓(不管方向/大小/手动自动) → 跳过此信号
    # 根因消除: 双重平仓竞态 / position_sync幽灵复活 / _close_in_progress覆盖
    # 旧逻辑(反向拦截+20%敞口)过于复杂且漏场景, 简化为"有就跳过"
    try:
        _adapter = getattr(getattr(scheduler, 'execution_engine', None), 'broker_adapter', None)
        if _adapter and hasattr(_adapter, 'get_positions'):
            _broker_pos = _adapter.get_positions() or []
            for _bp in _broker_pos:
                if _bp.get("symbol") == symbol and float(_bp.get("qty", 0)) != 0:
                    _qty = abs(float(_bp["qty"]))
                    _dir = "LONG" if float(_bp["qty"]) > 0 else "SHORT"
                    logger.info(
                        f"⏭️ [持仓去重] {symbol} 已有 {_dir} {_qty:.0f}股, 跳过此信号"
                    )
                    return
    except Exception as _rv_err:
        # fail-close: broker 查询失败时不交易 (安全优先)
        logger.warning(f"🔒 [持仓去重·fail-close] {symbol} broker 查询异常，跳过下单: {_rv_err}")
        return

    # ── P0 (2026-06-14): 时段门禁 — opening_drive 禁止入场 ──
    # 数据驱动: 136 笔 opening_drive 退出, 18.4% WR, -366.6% 累计亏损 (占总亏损 116%)
    # 开盘前30分钟波动 2-3x ATR, 信号在此时段入场 ≈ 送钱
    try:
        from app.core.shared_kernel import get_shared_kernel
        _current_session = get_shared_kernel().get_current_session()
        if _current_session == "opening_drive":
            logger.info(
                f"⛔ [时段门禁] {symbol} opening_drive 禁止入场 "
                f"(数据: 136笔 -366.6%, 信号已记录但不执行)"
            )
            return
    except Exception as _sess_err:
        logger.debug(f"[时段门禁] 检查异常 (放行): {_sess_err}")

    # ── H3 (2026-06-03): 退出屏障 — 线程安全检查 ──
    try:
        from app.pipeline.position_monitor import _check_exit_barrier, _EXIT_BARRIER_SECONDS
        if _check_exit_barrier(symbol):
            logger.warning(
                f"🚫 [退出屏障] {symbol} {_EXIT_BARRIER_SECONDS}s 内刚平仓，拒绝入场 "
                f"(防 flash_exit→再入场 死循环)"
            )
            return
    except Exception as _be:
        logger.debug(f"[退出屏障] 检查异常 (放行): {_be}")

    # ── P0 (2026-06-02): 系统级 FLASH_EXIT 熔断 ──
    # 数据驱动: 今日 83 笔 FLASH_EXIT 累计 -$14,251 (XLI 单只 24 次)
    # 机制: 10 分钟内 FLASH_EXIT >= 5 次 → 全局暂停开仓 10 分钟
    if _check_flash_exit_circuit_breaker(symbol):
        return

    # ── P1 (2026-06-02): 信号冷却二次检查 — 防并发扫描遗漏 ──
    # 外层 signal_pipeline 在循环开始时检查过 cooldown, 但退出可能在扫描进行中发生
    try:
        if hasattr(scheduler, '_is_in_cooldown') and scheduler._is_in_cooldown(symbol):
            logger.debug(f"🚫 [冷却复检] {symbol} 信号冷却中，拒绝入场")
            return
    except Exception:
        pass

    # ── P0 (2026-06-02): 标的日亏损限额 — 防死亡螺旋 ──
    # 同一标的当日累计亏损 >= $500 时当日禁入
    if not _check_symbol_daily_loss(symbol):
        return

    # 信号质量已在 _check_gates_and_conflicts() SSOT 门禁统一检查

    # ─── SSOT (2026-06-06): 统一安全门禁 — 预估算快速拒绝 ───
    # 用 signal.quantity 或默认股数做快速预检，避免后续昂贵计算
    # 真实股数校验在 _get_order_qty 中做第二次（defense in depth）
    try:
        from app.risk.pre_trade_gate import pre_trade_safety_check
        _sig_dir = getattr(signal, 'direction', None)
        _dir_str = _sig_dir.value if hasattr(_sig_dir, 'value') else str(_sig_dir or "LONG")
        _est_price = float(getattr(signal, 'price', 0) or 0)
        if _est_price <= 0:
            try:
                _est_price = float(context.get('current_price', 0) or context.get('price', 0) or 0)
            except Exception:
                pass
        if _est_price <= 0:
            try:
                from app.data.market_data_router import get_market_data_router
                _md = get_market_data_router().get_latest(symbol)
                if _md:
                    _est_price = float(_md.get('price', 0) or 0)
            except Exception:
                pass
        if _est_price > 0:
            _est_qty = int(getattr(signal, 'quantity', DEFAULT_ORDER_QTY) or DEFAULT_ORDER_QTY)
            _ps = getattr(scheduler, 'position_store', None)
            _ok, _mq, _rsn = pre_trade_safety_check(
                symbol=symbol, direction=_dir_str, quantity=_est_qty, price=_est_price,
                position_store=_ps,
                broker_adapter=getattr(getattr(scheduler, 'execution_engine', None), 'broker_adapter', None),
            )
            if not _ok and _mq <= 0:
                logger.warning(f"{_rsn}")
                return
    except Exception as _gate_err:
        # fail-close: 预估算异常时也拒绝，不依赖后续 _get_order_qty 兜底
        logger.error(f"🚫 [安全门禁·预估] {symbol} 检查异常 (拒绝): {_gate_err}")
        return

    # ─── P0 (2026-06-02): 广度信号 — Muyuan 论文核心发现 ───
    # ≥6 只同时超卖 → LONG strength+30, confidence×1.3, 禁止 SHORT
    # 论文数据: 恐慌时反转 alpha 11.6× 基线, 胜率 78%
    _breadth_boost = {}
    try:
        from app.analysis.market_breadth import get_market_breadth
        _breadth = get_market_breadth()
        _breadth_boost = _breadth.get_signal_boost()
        _sig_dir = getattr(signal, 'direction', None)
        _dir_str = _sig_dir.value if hasattr(_sig_dir, 'value') else str(_sig_dir or "")

        # 恐慌/放大模式下禁止 SHORT (反弹太暴力, 做空逆势)
        if _breadth_boost.get("block_short") and _dir_str == "SHORT":
            logger.warning(
                f"🚫 [广度·{_breadth_boost['tier'].upper()}] {symbol} 禁止做空: "
                f"{_breadth_boost['oversold_count']} 只同时超卖, 反转概率极高"
            )
            return

        # LONG 信号增强
        if _dir_str == "LONG" and _breadth_boost.get("strength_bonus", 0) > 0:
            _old_str = signal.strength
            _old_conf = signal.confidence
            signal.strength = min(100, signal.strength + _breadth_boost["strength_bonus"])
            signal.confidence = min(0.95, signal.confidence * _breadth_boost["confidence_mult"])
            # 标记为反转信号 (用于后续止损调整和 entry_reason 标记)
            if not hasattr(signal, 'metadata') or signal.metadata is None:
                signal.metadata = {}
            signal.metadata["breadth_boosted"] = True
            signal.metadata["breadth_tier"] = _breadth_boost["tier"]
            signal.metadata["breadth_oversold_count"] = _breadth_boost["oversold_count"]
            logger.info(
                f"📊 [广度·{_breadth_boost['tier'].upper()}] {symbol} LONG 信号增强: "
                f"strength {_old_str}→{signal.strength}, "
                f"confidence {_old_conf:.3f}→{signal.confidence:.3f}, "
                f"超卖={_breadth_boost['oversold_count']}只"
            )
    except Exception as _bre:
        logger.debug(f"[广度] 检查异常 (不影响入场): {_bre}")

    # 0. PDT 保护门禁 (P0-fix 2026-05-02: 集成到执行路径)
    # 模拟盘也守 PDT (2026-06-24): sim 验证 PDT 合规策略, 避免 sim 达标 live 跑不了的假达标
    try:
        # sim/live 统一查 PDT (is_pdt_account: equity≥25k 豁免, <25k 守 ≤3/5日)
        is_overnight = signal.direction in (Direction.LONG,)  # SHORT 总是日内
        allowed, reason, _level = _get_pdt_guard().check_trade_permission(is_overnight=is_overnight)
        if not allowed:
            logger.warning(f"🚫 [PDT 拦截] {symbol}: {reason}")
            return
    except Exception as e:
        logger.debug(f"[PDT] 检查异常 (放行): {e}")
    
    # 0.5 P0 修复 (2026-05-08): 标的级冷却 — STRUCTURE_EXIT 连续亏损冷却
    if not _check_symbol_cooldown(symbol, signal.direction):
        return

    # F1 (2026-05-16): Whipsaw 反向冷却 — 平仓后 N 秒内禁止反向开仓
    # 实证: 5/14 SNDK SHORT→LONG 90s 亏损, 5/15 CRM LONG→SHORT 173s 亏损
    # 价格随机游走时, 反向追单大概率被吃损
    if not _check_reversal_cooldown(scheduler, symbol, signal.direction):
        return

    # Task #50 (2026-05-28): symbol+direction 级互斥锁
    # 在 coordinator 仲裁前先做 (symbol, direction) 级阻断:
    # - 避免多策略在同一标的同方向重复入场
    # - 不同方向 (对冲) 仍放行
    _sym_dir_owner = "main"
    try:
        _sig_meta = getattr(signal, "metadata", None) or {}
        _sym_dir_owner = str(_sig_meta.get("owner_strategy") or "main")
    except Exception as _silent_e:
        logger.debug(f"[silent_except:order_executor.py] {_silent_e}")
    try:
        from app.execution.symbol_lock_manager import get_symbol_lock_manager
        _sym_dir_lock = get_symbol_lock_manager()
        _dir_str = signal.direction.value if hasattr(signal.direction, "value") else str(signal.direction)
        if not _sym_dir_lock.try_acquire(symbol, _dir_str, _sym_dir_owner):
            logger.warning(
                f"[信号拒绝] {symbol} {_dir_str}: 被其他策略占有 "
                f"(holder={_sym_dir_lock.is_locked(symbol, _dir_str)}), {_sym_dir_owner} 跳过"
            )
            return
        # 标记本次持锁, 出口/平仓需释放
        context["_symdir_lock_acquired"] = True
        context["_symdir_lock_owner"] = _sym_dir_owner
        context["_symdir_lock_dir"] = _dir_str
    except Exception as _sde:
        logger.debug(f"[SymbolLock·symdir] acquire 异常 (放行): {_sde}")

    # 0.7 Phase A (2026-05-14): Coordinator 多源信号采集 + 仲裁审计 (无副作用)
    # T4 (2026-05-16): sandbox enforce — 真实阻断 SymbolLock 冲突
    # B.8: 让所有已注册 adapter 都生成候选信号 (Phase A: 大多数 adapter 返回 None)
    #      然后 arbitrate 审计写表.
    try:
        _coord = getattr(scheduler, "coordinator", None)
        if _coord is not None and _coord.enabled:
            _adapters = getattr(scheduler, "_strategy_adapters", []) or []
            # 主 adapter 必装 (主线信号已经生成, 直接挂)
            _main_adapter = next((a for a in _adapters if a.name == "main"), None)
            if _main_adapter is None:
                from app.strategy.adapters import MainPipelineAdapter
                _main_adapter = MainPipelineAdapter()
            _candidates = [(_main_adapter, signal)]
            # 其他 adapter (Phase A: 返回 None 不参与, Phase B: 真实生成)
            for _a in _adapters:
                if _a.name == "main":
                    continue
                try:
                    _other_sig = _a.generate_signal(symbol, context)
                    if _other_sig is not None:
                        _candidates.append((_a, _other_sig))
                except Exception as _adapter_err:
                    logger.debug(f"[Coordinator] adapter {_a.name} 信号生成失败 (跳过): {_adapter_err}")
            # 写审计 (即使 1 候选也写) — T8b: 传 ctx 让 effective_priority 走 regime-aware
            _coord.arb.arbitrate(symbol, _candidates, ctx=context)
            if len(_candidates) > 1:
                logger.info(
                    f"[Coordinator·multi] {symbol} 多源候选 n={len(_candidates)}: "
                    f"{[a.name for a, _ in _candidates]}"
                )

            # T4: enforce 模式下 SymbolLock 真实仲裁
            # 主 adapter 申请 lock, 已被其他 owner 持有 → DENIED (signal_arbitration 已留痕)
            if not _coord.is_observe_only():
                try:
                    _lock = _coord.lock
                    _ok = _lock.acquire(symbol, "main")
                    if not _ok:
                        _holder = _lock.owner(symbol) or "?"
                        logger.warning(
                            f"[T4·SymbolLock·ENFORCE] {symbol} held by {_holder}, "
                            f"main REJECTED — 跳过主线下单"
                        )
                        return  # 拒下单, 不进入 _check_gates_and_conflicts
                    # R1 (2026-05-16): 标记本次持锁, 出口必释放 (除非 _close_position 接管)
                    context["_r1_lock_acquired"] = True
                except Exception as _le:
                    logger.debug(f"[T4·SymbolLock] 异常 (放行): {_le}")
    except Exception as _ce:
        logger.debug(f"[Coordinator] multi-source/arbitrate 审计失败 (放行): {_ce}")

    # R1 (2026-05-16): 锁泄漏修复 — 4 个 return 路径必释放 lock
    # 仅 _execute_long/short 成功创建仓位后, lock 归 on_position_closed 释放;
    # 否则下面任意 return 都要主动 release.
    def _r1_release_if_held():
        try:
            if context.get("_r1_lock_acquired") and not context.get("_r1_position_created"):
                _coord = getattr(scheduler, "coordinator", None)
                if _coord is not None and _coord.enabled and not _coord.is_observe_only():
                    _coord.lock.release(symbol, "main")
                    logger.debug(f"[R1·lock·release] {symbol} 主线 (未成单) 主动释放")
        except Exception as _re:
            logger.debug(f"[R1·release] 异常: {_re}")
        # Task #50: symbol+direction 锁同步释放 (创仓失败路径)
        try:
            if context.get("_symdir_lock_acquired") and not context.get("_r1_position_created"):
                from app.execution.symbol_lock_manager import get_symbol_lock_manager
                get_symbol_lock_manager().release(
                    symbol,
                    context.get("_symdir_lock_dir", ""),
                    context.get("_symdir_lock_owner", "main"),
                )
                context["_symdir_lock_acquired"] = False
        except Exception as _sre:
            logger.debug(f"[SymbolLock·symdir·release] 异常: {_sre}")

    try:
        # 1. 风控门禁与冲突检查
        if not _check_gates_and_conflicts(scheduler, symbol, signal, context):
            _r1_release_if_held()
            return

        # 2. 金字塔加仓处理
        if _handle_pyramid_add(scheduler, symbol, signal, context):
            # 金字塔加仓不算新仓 (原仓还在), lock 仍归原仓平仓时释放
            return

        # 2.5 P0 (2026-06-02): 同方向重复开仓拦截
        # 金字塔未处理 + position_store 已有同向持仓 = 信号管道并发导致的重复入场
        # 案例: RIVN 22:11 同时开 6 仓 (qty=360×2)
        try:
            if scheduler.position_store.has(symbol):
                _existing = scheduler.position_store.get(symbol)
                _existing_dir = _existing.direction.value if hasattr(_existing.direction, 'value') else str(_existing.direction)
                _signal_dir = signal.direction.value if hasattr(signal.direction, 'value') else str(signal.direction)
                if _existing_dir == _signal_dir:
                    logger.warning(
                        f"🚫 [多仓拦截] {symbol} 已有同向{_existing_dir}持仓 "
                        f"(qty={_existing.size}), 金字塔未触发, 拒绝重复开仓"
                    )
                    _r1_release_if_held()
                    return
        except Exception:
            pass

        # ── P0 (2026-06-17): CapitalAllocator enforce — 策略级资金上限强制执行 ──
        # 审计发现: try_reserve() 从未在下单路径调用, 策略级资金上限形同虚设
        # 修复: 在方向路由前强制调用 try_reserve, 超限直接拒绝
        try:
            from app.coordinator.capital_allocator import get_capital_allocator
            _ca = get_capital_allocator()
            if not _ca.is_observe_only():
                _strat_id = ""
                if voting_result:
                    _strat_id = voting_result.get("strategy_id", "") or voting_result.get("model", "")
                if not _strat_id and hasattr(signal, "metadata") and signal.metadata:
                    _strat_id = signal.metadata.get("strategy_id", "main")
                if not _strat_id:
                    _strat_id = "main"
                # 估算仓位占比: 用 signal.position_pct 或保守默认值
                _pos_pct = float(getattr(signal, "position_pct", 0) or 0)
                if _pos_pct <= 0:
                    # 从 context 或默认值估算
                    _pos_pct = float(context.get("estimated_position_pct", 0.10))
                if not _ca.try_reserve(_strat_id, _pos_pct):
                    logger.warning(
                        f"🚫 [CapitalAllocator·ENFORCE] {symbol} 策略={_strat_id} "
                        f"资金预留 {_pos_pct:.1%} 被拒 (bucket/total cap 超限)"
                    )
                    _r1_release_if_held()
                    return
        except Exception as _ca_err:
            # CapitalAllocator 异常 → 记录但不阻断 (enforce 模式下的 fail-close 由 allocator 自身处理)
            logger.warning(f"[CapitalAllocator] {symbol} 预留检查异常 (放行): {_ca_err}")

        # 3. 按方向路由执行
        if signal.direction == Direction.SHORT:
            _execute_short(scheduler, symbol, signal, context, voting_result)
        else:
            _execute_long(scheduler, symbol, signal, context, voting_result)

        # 如果上面没标 _r1_position_created → 创仓失败, 主动释放
        if not context.get("_r1_position_created"):
            _r1_release_if_held()
    except Exception as _ee:
        _r1_release_if_held()
        raise


# ═══════════════════════════════════════════════════════════════════
#  平仓执行
# ═══════════════════════════════════════════════════════════════════



def _lookup_decision_id_by_trace(trace_id: str, symbol: str = "") -> str:
    """
    通过 trace_id 从 trade_attribution DB 反查 decision_id。
    用于平仓时无法从 position.metadata 获取 decision_id 的 fallback。
    Returns: decision_id or empty string.
    """
    if not trace_id:
        return ""
    try:
        import sqlite3
        from app.core.paths import get_trade_records_db
        with sqlite3.connect(str(get_trade_records_db())) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT decision_id FROM trade_attribution "
                "WHERE trace_id = ? AND decision_id IS NOT NULL AND decision_id != '' "
                "ORDER BY created_at DESC LIMIT 1",
                (trace_id,)
            )
            row = cursor.fetchone()
            return str(row[0]) if row and row[0] else ""
    except Exception as e:
        logger.debug(f"[归因] _lookup_decision_id_by_trace 查询失败: {e}")
        return ""


def _build_close_metadata_json(meta: dict, position) -> Optional[str]:
    """构建平仓时的 metadata_json (完整归因 + MFE/MAE + 执行元数据)

    P0 归因修复 (2026-06-06): 保留 position.metadata 全部归因字段
    此前只保留 execution 元数据 + MFE/MAE, 导致 trace_id/factor_detail/signal_tags
    等关键归因字段在平仓时被丢弃 → trade_records.metadata_json 几乎为空

    现在: 先复制 meta (position.metadata) 全量, 再叠加 execution + MFE/MAE
    """
    # 1. 复制 position.metadata 全量 (含 trace_id, factor_detail, signal_tags, entry_reason 等)
    _result = dict(meta) if isinstance(meta, dict) else {}

    # 2. 叠加 execution 元数据 (TWAP/滑点/延迟)
    _exec = meta.get("execution", {}) if isinstance(meta.get("execution"), dict) else {}
    if _exec:
        _result["execution"] = _exec

    # 3. 附加 MFE/MAE (反馈闭环)
    _mfe = getattr(position, 'max_favorable_excursion', 0.0) or 0.0
    _mae = getattr(position, 'max_adverse_excursion', 0.0) or 0.0
    if _mfe != 0.0 or _mae != 0.0:
        _result["max_favorable_excursion"] = _mfe
        _result["max_adverse_excursion"] = _mae
    return json.dumps(_result, ensure_ascii=False, default=str) if _result else None


def _derive_entry_reason_for_close(meta: dict) -> str:
    """P0 修复 (2026-05-27): 平仓时推导 entry_reason

    优先级:
      1. position.metadata['entry_reason'] (开仓时已写入)
      2. 从 strategy_id + source_type 推导
      3. 兜底 'unknown'
    """
    from app.core.models import derive_entry_reason

    # 1. 直接读取开仓时写入的 entry_reason
    existing = meta.get("entry_reason", "")
    if existing and existing != "None":
        return existing

    # 2. 推导
    strategy_id = meta.get("strategy_id", "")
    source = meta.get("source", "") or meta.get("source_type", "")
    # source_type 为来源分类, 如 MAIN_PIPELINE/ORB_ENGINE/PLAYBOOK_SHADOW 等
    return derive_entry_reason(
        strategy_id=strategy_id,
        source=source,
        metadata=meta,
    )


def _fetch_attribution_data(trace_id: str, symbol: str = "", trade_date: str = "", direction: str = ""):
    """
    Fallback: query trade_attribution DB for signal data when position.metadata is empty.
    This handles the case where metadata is lost due to service restart or sync overwrite.
    Tries trace_id first, then falls back to symbol+date+direction matching.
    Returns: (signal_strength, signal_confidence) or (0, 0.0) if not found.
    """
    import sqlite3
    from app.core.paths import get_trade_records_db
    db_path = get_trade_records_db()
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.cursor()
            # F19 (2026-05-12): schema 已由 TradeStore._ensure_trade_attribution_schema 集中管理
            # Primary: match by trace_id
            if trace_id:
                cursor.execute(
                    "SELECT signal_strength, signal_confidence FROM trade_attribution WHERE trace_id = ?",
                    (trace_id,)
                )
                row = cursor.fetchone()
                if row and row[0] is not None and row[0] > 0:
                    return int(row[0]), float(row[1] or 0.0)
            # Fallback: match by symbol + trade_date + direction (when trace_id is empty)
            if symbol and trade_date:
                if direction:
                    cursor.execute(
                        "SELECT signal_strength, signal_confidence FROM trade_attribution "
                        "WHERE symbol = ? AND trade_date = ? ORDER BY created_at DESC LIMIT 1",
                        (symbol, trade_date)
                    )
                else:
                    cursor.execute(
                        "SELECT signal_strength, signal_confidence FROM trade_attribution "
                        "WHERE symbol = ? AND trade_date = ? ORDER BY created_at DESC LIMIT 1",
                        (symbol, trade_date)
                    )
                row = cursor.fetchone()
                if row and row[0] is not None and row[0] > 0:
                    return int(row[0]), float(row[1] or 0.0)
    except Exception as e:
        logger.debug(f"[归因回填] 查询 trade_attribution 失败：{e}")
    return 0, 0.0


def _fetch_decision_data(symbol: str, trade_date: str = "", direction: str = ""):
    """
    Task #42 (P0, 2026-05-28): 投票→交易记录数据链路修复 — decisions.db 兜底查询.

    当 position.metadata 丢失且 trade_attribution 也查不到 (例如 trace_id 为空)
    时, 从 voting_coordinator 写入的 decisions.db 反查同一 (symbol, 交易日,
    方向) 组合的最近一条决策, 补出 final_strength / consensus_level / signal_strength.

    P1 修复 (2026-05-31): 新增 signal_snapshots.db 作为第 4 层兜底.
    当 decisions.db 也查不到时, 从 signal_snapshots 按 (symbol, trade_date) 匹配.

    Returns:
        dict {
            "signal_strength": int,    # = decisions.final_strength (以投票后强度为准)
            "signal_confidence": float, # = consensus_level
            "final_strength": int,
            "consensus_level": float,
            "decision_id": str,
        } 或空 dict (未命中).
    """
    import sqlite3
    try:
        from app.core.paths import get_decisions_db
        db_path = get_decisions_db()
    except Exception:
        return {}
    if not symbol:
        return {}
    # 规范化方向 (decisions.direction 存为 LONG/SHORT/NEUTRAL)
    _dir = (direction or "").upper().strip()
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.cursor()
            # 优先: symbol + trade_date + direction
            params = [symbol]
            sql = ("SELECT decision_id, final_strength, consensus_level, direction "
                   "FROM decisions WHERE symbol = ?")
            if trade_date:
                sql += " AND date(created_at) = ?"
                params.append(trade_date)
            if _dir and _dir not in ("NEUTRAL", ""):
                sql += " AND direction = ?"
                params.append(_dir)
            sql += " ORDER BY created_at DESC LIMIT 1"
            row = cursor.execute(sql, tuple(params)).fetchone()
            if not row and (_dir or trade_date):
                # 退化: 仅匹配 symbol 取最近一条 (避免 trade_date 跨天/方向不一致漏掉)
                row = cursor.execute(
                    "SELECT decision_id, final_strength, consensus_level, direction "
                    "FROM decisions WHERE symbol = ? ORDER BY created_at DESC LIMIT 1",
                    (symbol,)
                ).fetchone()
            if row and row[1] is not None:
                return {
                    "decision_id":      row[0] or "",
                    "final_strength":   int(row[1] or 0),
                    "consensus_level":  float(row[2] or 0.0),
                    # decisions.db 中 final_strength 即为投票后强度, 以此作为 signal_strength 兜底
                    "signal_strength":  int(row[1] or 0),
                    "signal_confidence": float(row[2] or 0.0),
                    "direction":        row[3] or "",
                }
    except Exception as e:
        logger.debug(f"[Task#42][decisions 兜底] {symbol} 查询失败: {e}")

    # ── P1 第 4 层兜底 (2026-05-31): signal_snapshots.db ──
    # 当 decisions.db 也查不到时, 从 signal_snapshots 按 (symbol, trade_date) 匹配
    try:
        from app.core.paths import get_data_dir
        snap_db = get_data_dir() / "signal_snapshots.db"
        if snap_db.exists():
            with sqlite3.connect(str(snap_db)) as conn:
                cursor = conn.cursor()
                snap_params = [symbol]
                snap_sql = ("SELECT decision_id, strength, confidence, direction "
                           "FROM signal_snapshots WHERE symbol = ?")
                if trade_date:
                    snap_sql += " AND trade_date = ?"
                    snap_params.append(trade_date)
                if _dir and _dir not in ("NEUTRAL", ""):
                    snap_sql += " AND direction = ?"
                    snap_params.append(_dir)
                snap_sql += " ORDER BY created_at DESC LIMIT 1"
                snap_row = cursor.execute(snap_sql, tuple(snap_params)).fetchone()
                if not snap_row and trade_date:
                    snap_row = cursor.execute(
                        "SELECT decision_id, strength, confidence, direction "
                        "FROM signal_snapshots WHERE symbol = ? AND trade_date = ? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (symbol, trade_date)
                    ).fetchone()
                if snap_row and snap_row[1] is not None:
                    logger.info(
                        f"[P1·signal_snapshots 兜底] {symbol} {trade_date} "
                        f"strength={snap_row[1]} decision_id={snap_row[0]}"
                    )
                    return {
                        "decision_id":      snap_row[0] or "",
                        "final_strength":   int(snap_row[1] or 0),
                        "consensus_level":  float(snap_row[2] or 0.0),
                        "signal_strength":  int(snap_row[1] or 0),
                        "signal_confidence": float(snap_row[2] or 0.0),
                        "direction":        snap_row[3] or "",
                    }
    except Exception as e:
        logger.debug(f"[P1·signal_snapshots 兜底] {symbol} 查询失败: {e}")

    return {}


def _insert_trade_attribution(
    cursor, trace_id, symbol, strength=0, confidence=0.0,
    # T2 (2026-05-16): 写齐因子 5 列 + 元数据 — 入场时落, 平仓时 UPDATE 补 exit_*
    # 5/15 100% NULL → 复盘看不到哪个因子主导
    factor_detail: dict = None,
    owner_strategy: str = None,
    direction: str = None,
    entry_time: str = None,
    entry_price: float = 0.0,
    quantity: int = 0,
    regime: str = None,
    session: str = None,
    vix_at_entry: float = 0.0,
    signal_tags: str = None,
):
    """Insert a baseline attribution record for a new trade

    T2 扩列: 写入因子明细 + 入场元数据 (owner_strategy/regime/session/vix/signal_tags)
    """
    try:
        fd = factor_detail or {}
        ft = (fd.get("trend") or {}).get("raw")
        fm = (fd.get("momentum") or {}).get("raw")
        fv = (fd.get("volume") or {}).get("raw")
        fo = (fd.get("orderflow") or {}).get("raw")
        fl = (fd.get("lifecycle") or {}).get("raw")
        # 2026-06-30: trade_date 用 ET 日期 (date('now') 是 UTC, 与 trade_records.trade_date 不一致)
        try:
            from app.utils.timezone import _et_trade_date, now_et as _now_et
            _attr_trade_date = _et_trade_date(_now_et())
        except Exception:
            _attr_trade_date = None
        cursor.execute('''
            INSERT INTO trade_attribution
            (trace_id, trade_date, symbol, direction,
             signal_strength, signal_confidence,
             entry_time, entry_price, quantity,
             owner_strategy, regime, session, vix_at_entry, signal_tags,
             factor_trend, factor_momentum, factor_volume, factor_orderflow, factor_lifecycle)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            trace_id, _attr_trade_date or '', symbol, direction or "LONG",
            int(strength or 0), float(confidence or 0.0),
            entry_time, float(entry_price or 0.0), int(quantity or 0),
            owner_strategy, regime, session, float(vix_at_entry or 0.0), signal_tags,
            ft, fm, fv, fo, fl,
        ))
    except Exception as e:
        logger.warning(f"插入归因基准记录失败 (T2): {e}")


def _update_trade_attribution(
    trace_id: str,
    symbol: str,
    pnl: float,
    pnl_pct: float,
    exit_reason: str,
    holding_hours: float,
    signal_strength: int = 0,
    signal_confidence: float = 0.0,
    max_drawdown: float = 0.0,
):
    """
    交易平仓后，更新 trade_attribution 表的归因数据。
    与 HK 标准保持一致: 记录结果标签、盈亏类别、退出原因。
    """
    import sqlite3
    from app.core.db_utils import get_main_db

    db_path = get_main_db()
    
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.cursor()
            
            # P0 修复 (F19 2026-05-12): 确保 trade_attribution 表存在
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_attribution (
                    attribution_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT,
                    trade_date TEXT,
                    symbol TEXT,
                    direction TEXT DEFAULT 'LONG',
                    signal_strength INTEGER DEFAULT 0,
                    signal_confidence REAL DEFAULT 0.0,
                    actual_price REAL DEFAULT 0.0,
                    pnl REAL DEFAULT 0.0,
                    pnl_category TEXT,
                    exit_reason TEXT,
                    result_tags TEXT,
                    direction_correct INTEGER,
                    strategy_fit INTEGER DEFAULT 0,
                    overall_score INTEGER DEFAULT 60,
                    success_factors TEXT,
                    failure_factors TEXT,
                    max_drawdown REAL DEFAULT 0.0,
                    holding_hours REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            
            # Check if row exists
            cursor.execute("SELECT trace_id FROM trade_attribution WHERE trace_id = ?", (trace_id,))
            if cursor.fetchone() is None:
                _insert_trade_attribution(cursor, trace_id, symbol, signal_strength, signal_confidence)

            # 1. 确定结果标签
            is_win = pnl > 0
            pnl_category = "win" if is_win else "loss"
            result_tags = json.dumps({
                "pnl_category": pnl_category,
                "exit_reason": exit_reason,
                "is_win": is_win,
            })
            
            # 2. 确定策略拟合度 (基于盈亏与置信度的关系)
            strategy_fit = 1 if (signal_confidence > 0.6 and is_win) else 0
            
            # 3. 计算综合评分 (0-100)
            base_score = 60
            pnl_bonus = min(30, abs(pnl_pct) * 100) if is_win else 0
            drawdown_penalty = max(0, max_drawdown * 10)
            final_score = max(0, min(100, int(base_score + pnl_bonus - drawdown_penalty)))
            
            # 4. 更新记录
            # RFC-050 (P1-1 fix): 移除不存在的 max_drawdown 列, holding_hours→holding_minutes
            _holding_min = int(holding_hours * 60) if holding_hours else 0
            cursor.execute('''
                UPDATE trade_attribution 
                SET actual_price = ?, 
                    pnl = ?,
                    pnl_category = ?,
                    exit_reason = ?,
                    result_tags = ?,
                    direction_correct = ?,
                    strategy_fit = ?,
                    overall_score = ?,
                    success_factors = ?,
                    failure_factors = ?,
                    signal_strength = COALESCE(?, signal_strength),
                    signal_confidence = COALESCE(?, signal_confidence),
                    holding_minutes = ?,
                    timestamp = CURRENT_TIMESTAMP
                WHERE trace_id = ?
            ''', (
                0.0, 
                pnl,
                pnl_category,
                exit_reason,
                result_tags,
                1 if is_win else 0,
                strategy_fit,
                final_score,
                json.dumps({"exit_reason": exit_reason}),
                json.dumps({"max_drawdown": max_drawdown}) if not is_win else "",
                signal_strength or None,
                signal_confidence or None,
                _holding_min,
                trace_id
            ))
            
            conn.commit()
            logger.debug(f"✅ 归因更新：{trace_id} | PnL={pnl:.2f} | Score={final_score}")

            # 2026-08-22 (结案A1): voting_predictions 回填块删除 —
            # 三模型投票架构 2026-07-12 已废 (表最后写入 7/12), 此块是唯一活写入方.
            # 旧信号链接线 (scan_loop) 保持配置静默不动 (稳定性边界), 仅断死表写入.
            # 历史 1943 行留档作投票期校准证据, 不再回填.

    except Exception as e:
        logger.warning(f"更新归因记录失败：{e}")


def _is_option_contract(symbol: str) -> bool:
    """判断是否为美股期权合约 (OCC 格式: US.XXXXXXP/CCCCCCCC)"""
    # 期权格式示例: US.CRM260515P180000, US.AAPL260515C180000
    parts = symbol.split(".")
    if len(parts) < 2:
        return False
    ticker_part = parts[-1]  # e.g. "CRM260515P180000"
    # 期权 OCC 格式: 6位到期日(YYMMDD) + P/C + 8位行权价
    # 最小长度: "AAPL260515P180000" = 17 chars
    if len(ticker_part) < 15:
        return False
    return "P" in ticker_part[6:10] or "C" in ticker_part[6:10]


def _get_contract_multiplier(symbol: str) -> int:
    """获取合约乘数: 美股期权=100, 股票=1"""
    if _is_option_contract(symbol):
        return 100
    return 1


def _verify_position_still_held(scheduler, position: Position) -> bool:
    """执行前校验持仓是否仍存在 (2026-06-06: position_store 内存查询替代 Futu API)。
    
    防止双重平仓: 如果 algo/手动已在外部门平仓，系统不再提交卖单。
    
    P1 性能修复 (2026-06-06): 用 position_store 内存查询替代 Futu position_list_query
      原: 每次平仓调 Futu API (50-200ms 网络延迟)
      现: position_store.has() 内存查询 (<1ms)
      安全性: position_sync 30s 刷新 + _close_in_progress 防并发重入
    
    Returns:
        True = 持仓仍在且方向一致，可执行平仓
        False = 持仓不存在/方向冲突，跳过
    异常时返回 True (保守放行，系统策略仓位需要退出保护)
    """
    try:
        _ps = getattr(scheduler, 'position_store', None)
        if not _ps:
            return True  # 无 position_store，保守放行
        
        # 检查持仓是否存在
        if not _ps.has(position.symbol):
            logger.warning(
                f"[持仓校验] {position.symbol} position_store 中无此持仓, 跳过平仓"
            )
            return False
        
        # 方向校验: position_store 中的方向 vs 待平仓的方向
        stored = _ps.get(position.symbol)
        if stored:
            stored_dir = stored.direction.name if hasattr(stored.direction, 'name') else str(stored.direction)
            close_dir = position.direction.name if hasattr(position.direction, 'name') else str(position.direction)
            if stored_dir != close_dir:
                logger.error(
                    f"[持仓校验·方向冲突🔴] {position.symbol} "
                    f"store={stored_dir} vs close_request={close_dir} — 禁止平仓"
                )
                return False
        
        return True
    except Exception as e:
        logger.debug(f"[持仓校验] 异常 (保守放行): {e}")
        return True


@_with_symbol_lock_release
def _close_position(scheduler, position: Position, current_price: float, exit_reason: ExitReason):
    """平仓 — 触及即全平，简洁优先"""
    # 2026-07-16 P0: 纵深防御 — 即使上游所有护栏被绕过, _close_position 也不平 Playbook 仓位.
    #   上游: position_sync/restore/reconcile/ExitDispatcher/DEM 全部跳过 Playbook.
    #   此处: 最后一道防线, 万一仓位漏网到此, 直接 return 不平仓.
    try:
        from app.playbook.pipeline import is_playbook_open_position
        if is_playbook_open_position(position.symbol):
            logger.warning(
                f"[平仓·playbook护栏] {position.symbol} 是未平 PLAYBOOK_SIM 仓, "
                f"跳过旧管线平仓 (由 monitor_playbook_sim_exits 自管)"
            )
            return
    except Exception:
        pass
    # C1 (2026-06-03): 并发重入保护 — 防止 8s 确认窗口内双重平仓
    # (原注释提及 ExitRuleDispatcher 双触发场景, 该路径已随架构体检 2026-08-12 删除;
    #  保护本身保留, 防御多线程/多循环并发平仓仍必要)
    meta = getattr(position, "metadata", {}) or {}

    # P0 (2026-06-07): 从 open_positions.metadata_json 恢复丢失的归因数据
    # 根因: position.metadata 仅存内存, 重启/position_sync 后清空
    # 修复: 入场时已将 metadata 持久化到 open_positions.metadata_json
    if not meta.get("factor_detail") and not meta.get("_factor_detail"):
        try:
            _ts = getattr(scheduler, 'trade_store', None)
            if _ts:
                _conn = _ts._get_conn()
                try:
                    _row = _conn.execute(
                        "SELECT metadata_json FROM open_positions WHERE symbol = ?",
                        (position.symbol,)
                    ).fetchone()
                    if _row and _row[0]:
                        _db_meta = json.loads(_row[0])
                        # 只补全缺失字段, 不覆盖内存中已有的值
                        for _k, _v in _db_meta.items():
                            if _k not in meta or not meta[_k]:
                                meta[_k] = _v
                        logger.debug(
                            f"[平仓·归因恢复] {position.symbol} 从 open_positions 恢复 "
                            f"{len(_db_meta)} 个 metadata 字段"
                        )
                finally:
                    _conn.close()
        except Exception as _meta_rec_err:
            logger.debug(f"[平仓·归因恢复] {position.symbol} 失败 (不影响平仓): {_meta_rec_err}")

    # ── P0 (2026-06-15): _close_blocked 检查 — broker 连续拒绝后停止重试 ──
    if meta.get("_close_blocked"):
        _reason = meta.get("_close_blocked_reason", "未知")
        logger.warning(
            f"[平仓·锁定] {position.symbol} 平仓已被锁定: {_reason} (跳过, 需人工介入)"
        )
        return

    # ── P0 (2026-06-12): _close_in_progress 超时自动清理 ──
    # 根因: 5个失败路径(异常/拒单/未确认/盘后/确认异常)都不清理此标记,
    #        导致一次平仓失败后仓位永久不可平仓直到重启
    # 修法: 改为 timestamp + 5分钟超时, 超过自动解锁允许重试
    # P0-fix (2026-06-15): 用 position_store._lock 保护 check-and-set 原子性
    _ps_lock = getattr(getattr(scheduler, 'position_store', None), '_lock', None)
    if _ps_lock:
        _ps_lock.acquire()
    try:
        _cip_ts = meta.get("_close_in_progress_ts", 0)
        _cip_timeout = 300  # 5 分钟
        if meta.get("_close_in_progress") and _cip_ts and (time.time() - _cip_ts < _cip_timeout):
            _remaining = int(_cip_timeout - (time.time() - _cip_ts))
            logger.warning(
                f"[平仓·重入拦截] {position.symbol} 已有平仓进行中 "
                f"({_remaining}s后自动解锁)，跳过"
            )
            return
        meta["_close_in_progress"] = True
        meta["_close_in_progress_ts"] = time.time()
        position.metadata = meta
    finally:
        if _ps_lock:
            _ps_lock.release()

    # Broker-first (2026-06-03): 执行前持仓校验 — 防双重平仓
    # 查 Futu 实时持仓，确认仓位还没被外部平仓 (algo/手动)
    if not _verify_position_still_held(scheduler, position):
        logger.warning(
            f"[平仓·持仓校验] {position.symbol} broker 端已无持仓或数量不足, "
            f"跳过平仓 (可能已被外部平仓)"
        )
        # 清理 _close_in_progress 标记
        meta = getattr(position, "metadata", {}) or {}
        meta.pop("_close_in_progress", None)
        position.metadata = meta
        return

    # P0 修复 (2026-06-04): 平仓前方向校验 — 防方向翻转导致平仓变加仓
    # 根因: 6/3 XLE 内存=LONG 但 Broker=SHORT, SELL 2780 变成加仓而非平仓
    # 验证: 内存方向与 Broker 实际方向一致, 不一致则修正内存后再平仓
    # P1 修复 (2026-06-06): broker 无此持仓时拦截平仓 — 补回 B4 内存化后的安全网
    try:
        _adapter = getattr(getattr(scheduler, 'execution_engine', None), 'broker_adapter', None)
        if _adapter and hasattr(_adapter, 'get_positions'):
            _broker_positions = _adapter.get_positions() or []
            _bp = None
            for _b in _broker_positions:
                _bsym = _b.get("symbol") if isinstance(_b, dict) else getattr(_b, "symbol", None)
                if _bsym == position.symbol:
                    _bp = _b
                    break
            if _bp is None:
                # broker 端无此持仓 — 可能已被外部平仓，拦截防止创建反向仓位
                logger.warning(
                    f"[平仓·broker无持仓🟡] {position.symbol} broker 端未找到此持仓, "
                    f"跳过平仓 (可能已被外部平仓, position_store 尚未同步)"
                )
                meta = getattr(position, "metadata", {}) or {}
                meta.pop("_close_in_progress", None)
                position.metadata = meta
                return
            _bqty = float(_bp.get("qty", 0) if isinstance(_bp, dict) else getattr(_bp, "qty", 0))
            _b_side = _bp.get("position_side", "") if isinstance(_bp, dict) else getattr(_bp, "position_side", "")
            _broker_is_short = _bqty < 0 or str(_b_side).upper() == "SHORT"
            _mem_is_short = position.direction == Direction.SHORT
            if _broker_is_short != _mem_is_short:
                    _old = position.direction.value if hasattr(position.direction, 'value') else str(position.direction)
                    _correct = Direction.SHORT if _broker_is_short else Direction.LONG
                    logger.critical(
                        f"🔴🔴🔴 [平仓·方向校验失败·P0] {position.symbol} "
                        f"内存={_old} vs Broker={'SHORT' if _broker_is_short else 'LONG'} "
                        f"→ 修正内存方向后继续平仓!"
                    )
                    position.direction = _correct
                    # 飞书告警
                    try:
                        from app.notifier.feishu import send_text
                        send_text(
                            f"🔴 [平仓方向校验·P0] {position.symbol}\n"
                            f"原内存={_old} → 修正为={'SHORT' if _broker_is_short else 'LONG'}\n"
                            f"broker_qty={_bqty}, 平仓继续执行"
                        )
                    except Exception:
                        pass
    except Exception as _dir_e:
        logger.warning(f"[平仓·方向校验] {position.symbol} 校验异常(不阻断): {_dir_e}")

    # ── Task #34 (P0, 2026-06-16): 平仓前安全检查 ──
    # 1) 退避冷却检查; 2) 强制刷新 broker 持仓 (0 则跳过下单);
    # 3) 检测/等待/取消待成交订单 (最多 60s, 5s 轮询)
    try:
        _skip_reason = _close_pre_check(scheduler, position)
        if _skip_reason:
            logger.warning(
                f"[平仓·安全检查] {position.symbol} {position.direction.value} "
                f"跳过下单: {_skip_reason}"
            )
            _meta = getattr(position, "metadata", {}) or {}
            _meta.pop("_close_in_progress", None)
            _meta.pop("_close_in_progress_ts", None)
            position.metadata = _meta
            return
    except Exception as _pcerr:
        logger.warning(f"[平仓·安全检查] {position.symbol} 异常(保守放行): {_pcerr}")

    # Task #14 (2026-05-27): 如果该标的开仓 TWAP 还在进行中, 先取消剩余片, 避免平后被动加仓
    try:
        if cancel_active_twap(position.symbol, reason=f"close:{exit_reason.value}"):
            logger.warning(
                f"[平仓·TWAP取消] {position.symbol} 剩余 TWAP 片已取消 (reason={exit_reason.value})"
            )
    except Exception as _ce:
        logger.debug(f"[平仓·TWAP取消] 异常(忽略): {_ce}")

    # 合约乘数 (美股期权标准乘数=100, 股票=1)
    contract_multiplier = _get_contract_multiplier(position.symbol)

    # 计算盈亏 (期权需 ×100 合约乘数)
    if position.direction == Direction.LONG:
        pnl = (current_price - position.avg_entry_price) * position.size * contract_multiplier
        pnl_pct = (current_price - position.avg_entry_price) / position.avg_entry_price if position.avg_entry_price > 0 else 0
    else:
        pnl = (position.avg_entry_price - current_price) * position.size * contract_multiplier
        pnl_pct = (position.avg_entry_price - current_price) / position.avg_entry_price if position.avg_entry_price > 0 else 0

    logger.info(
        f"[平仓] {position.symbol} reason={exit_reason.value} "
        f"PnL=${pnl:.2f} ({pnl_pct:.2%})"
    )

    # 构建平仓订单
    # 2026-06-10 红队修复: MARKET→LIMIT ± 2% 价格保护带, 防止闪崩时市价单成交在极端价格
    # 如果限价单未成交, 后续 scan 周期会以更新价格重试或降级为市价单
    _exit_slippage_band = 0.02  # 2% 滑点容忍
    if position.direction == Direction.LONG:
        _exit_limit_price = round(current_price * (1 - _exit_slippage_band), 2)
    else:
        _exit_limit_price = round(current_price * (1 + _exit_slippage_band), 2)
    close_order = Order(
        client_order_id=f"{position.symbol}_close_{int(time.time())}_{uuid.uuid4().hex[:6]}",
        signal_id=position.signal_id,
        symbol=position.symbol,
        direction=Direction.SHORT if position.direction == Direction.LONG else Direction.LONG,
        order_type=OrderType.LIMIT,
        quantity=position.size,
        price=_exit_limit_price,
    )
    # P1-2 修复 2026-04-26: 平仓订单标记 is_exit=True，跳过幂等检查避免重复拦截
    if not hasattr(close_order, 'metadata') or close_order.metadata is None:
        close_order.metadata = {}
    close_order.metadata['is_exit'] = True

    try:
        if scheduler.execution_engine:
            close_result = scheduler.execution_engine.submit_order(close_order)
            # P0 Fix 2026-04-28: 记录平仓单的 futu_order_id 到 position.metadata
            close_futu_id = getattr(close_result, "futu_order_id", "")
            if close_futu_id:
                meta = getattr(position, "metadata", {}) or {}
                meta["close_futu_order_id"] = close_futu_id
                position.metadata = meta
                logger.info(f"[平仓] 平仓单 futu_order_id={close_futu_id}")

            # PDT 日内交易记录 (P0 2026-06-11): 平仓时判断是否 round trip
            try:
                from app.risk.pdt_guard import get_pdt_guard
                _entry = getattr(position, 'entry_time', None)
                if _entry:
                    get_pdt_guard().record_close(position.symbol, _entry)
            except Exception as _pdt_err:
                logger.debug(f"[平仓] PDT 记录失败 (不影响平仓): {_pdt_err}")
    except Exception as e:
        # ── Task #34 (P0, 2026-06-16): 指数退避重试 ──
        # 2 次 → 5min 冷却; 3-4 次 → 30min 冷却; 5 次 → 永久阻塞
        # 仅"持仓不足/待成交订单"类错误走指数退避, 其它错误使用较短冷却
        meta = getattr(position, "metadata", None)
        if meta is None:
            meta = {}
            position.metadata = meta
        _err_msg = str(e)
        _is_backoff = _is_backoff_error(_err_msg)

        # ── P0 (2026-06-16): 幽灵仓即时收敛 ──
        # 根因: broker 说"持仓不足"= broker 端已无此仓位。
        # 修法: 查 broker 实际持仓, 为 0 则立即收敛 (移除 PositionStore + 标 anomalous),
        #        不再走退避重试循环。一条规则消灭整类幽灵仓问题。
        _ghost_insufficient = ("持仓不足" in _err_msg or "insufficient" in _err_msg.lower())
        if _ghost_insufficient:
            _broker_qty = _refresh_broker_position_qty(scheduler, position.symbol)
            if _broker_qty is not None and _broker_qty <= 0:
                logger.warning(
                    f"[平仓·幽灵收敛] {position.symbol} broker 确认无持仓 (qty=0), "
                    f"立即从 PositionStore 移除, 不再重试"
                )
                meta["_ghost_converged"] = True
                meta["_ghost_converged_at"] = now_et().isoformat()
                meta["_ghost_reason"] = _err_msg[:200]
                # 标记 DB anomalous + 移除内存
                try:
                    if hasattr(scheduler, "trade_store") and scheduler.trade_store:
                        scheduler.trade_store.upsert_open_position(
                            position, source="scheduler", action="ghost_converged"
                        )
                except Exception:
                    pass
                scheduler.position_store.remove(position.symbol)
                _clear_close_retry(position.symbol, position.direction)
                return

        # P0-fix: _record_close_failure 必须在 _blocked 引用前调用
        fail_count, cooldown_min, _blocked = _record_close_failure(
            scheduler, position, _err_msg, _is_backoff
        )

        if _blocked:
            meta["_close_blocked"] = True
            meta["_close_blocked_reason"] = (
                f"{'退避类' if _is_backoff else '其它'}错误累计 {fail_count} 次: {_err_msg[:200]}"
            )
            meta["_close_blocked_at"] = now_et().isoformat()
            logger.critical(
                f"[平仓·阻塞] {position.symbol} {position.direction.value} "
                f"连续 {fail_count} 次平仓失败 → 标记为异常持仓 (blocked=True), 需人工介入 (调用 manual_unblock). "
                f"最后错误: {_err_msg}"
            )
            try:
                if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
                    scheduler.trade_store.upsert_open_position(
                        position, source="scheduler", action="mark_anomalous"
                    )
            except Exception as db_err:
                logger.debug(f"[平仓·异常] DB 同步失败: {db_err}")
            try:
                if hasattr(scheduler, 'dingtalk') and scheduler.dingtalk:
                    scheduler.dingtalk.send_text(
                        f"🚨 P0 异常持仓告警 (Task #34)\n"
                        f"标的: {position.symbol}\n"
                        f"方向: {position.direction.value}\n"
                        f"数量: {position.size}\n"
                        f"入场价: ${position.avg_entry_price:.2f}\n"
                        f"当前价: ${current_price:.2f}\n"
                        f"PnL: ${pnl:.2f} ({pnl_pct:.2%})\n"
                        f"失败次数: {fail_count}\n"
                        f"错误类别: {'退避类' if _is_backoff else '其它'}\n"
                        f"最后错误: {_err_msg}\n"
                        f"操作: 请在 Dashboard 点击人工解锁或手动平仓"
                    )
            except Exception as notify_err:
                logger.debug(f"[平仓·告警] 钉钉通知失败: {notify_err}")
            try:
                from app.notifier.feishu import send_text as _feishu
                _feishu(
                    f"🚨 [平仓阻塞·Task#34] {position.symbol} {position.direction.value} "
                    f"连续{fail_count}次平仓失败→标记blocked\n原因: {_err_msg[:120]}"
                )
            except Exception:
                pass
        elif cooldown_min:
            logger.error(
                f"[平仓] {position.symbol} {position.direction.value} 下单失败 "
                f"({fail_count}/{_CLOSE_BLOCK_THRESHOLD}) → 冷却 {cooldown_min}min, "
                f"保留本地持仓状态: {e}"
            )
        else:
            logger.error(
                f"[平仓] {position.symbol} {position.direction.value} 下单失败 "
                f"({fail_count}/{_CLOSE_BLOCK_THRESHOLD})，保留本地持仓状态: {e}"
            )
        return

    # P0 根因修复 (2026-05-14): 使用 OrderTracker 追踪订单全生命周期
    # 根因: 订单提交后未确认就标记 pending_close 并发送通知，导致虚假平仓
    # 修复: 注册订单到 OrderTracker，只在确认成交后才执行后续清理和通知
    if close_futu_id:
        from app.risk.order_tracker import get_order_tracker, OrderState
        from app.core.scheduler_triggers import get_market_phase, MarketPhase
        
        tracker = get_order_tracker()
        is_after_hours = get_market_phase() in (MarketPhase.AFTER_HOURS, MarketPhase.PRE_MARKET)
        
        # 注册订单到追踪器
        tracker.register_order(
            futu_order_id=close_futu_id,
            symbol=position.symbol,
            direction=position.direction.value,
            quantity=position.size,
            exit_reason=exit_reason.value,
            is_after_hours=is_after_hours,
        )
        
        # 如果未确认成交，不标记 pending_close，不发送通知
        # 持仓继续被监控，订单状态由 OrderTracker 在下次扫描时检查
        if is_after_hours:
            logger.warning(
                f"[平仓·盘后] {position.symbol} 订单已提交但盘后无法确认成交，"
                f"futu_id={close_futu_id}，持仓继续监控，等待开盘后确认"
            )
            # 标记订单为待追踪，但不清理持仓
            meta["_close_order_submitted"] = True
            meta["_close_futu_order_id"] = close_futu_id
            meta["_close_exit_reason"] = exit_reason.value
            meta["_close_pnl"] = pnl
            meta["_close_pnl_pct"] = pnl_pct
            meta["_close_current_price"] = current_price
            position.metadata = meta
            
            # 同步到 DB（保持持仓状态）
            try:
                if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
                    scheduler.trade_store.upsert_open_position(
                        position, source="scheduler", action="close_order_pending"
                    )
            except Exception as e:
                logger.warning(f"[平仓·盘后] DB 同步失败：{e}")
            return  # 不执行后续的持仓清理和通知
    
        # 交易时段：等待确认成交
        try:
            from futu import RET_OK as RET_OK_FUTU, TrdEnv
            import time as _time

            trd_env_f = TrdEnv.SIMULATE if getattr(scheduler, 'trade_env', 1) == 1 else TrdEnv.REAL
            order_confirmed = False

            # P1 修复 (2026-06-02): 确认窗口从 2s 延长到 8s
            # 根因: 21 笔平仓单全部 2s 内未确认 (Futu 模拟盘需 4-6s)
            # 4 次轮询 × 2s 间隔 = 8s 窗口
            for _attempt in range(4):
                _time.sleep(2)
                ret_q, data_q = scheduler.execution_engine.broker_adapter.trade_ctx.order_list_query(
                    order_id=close_futu_id,
                    trd_env=trd_env_f
                )
                if ret_q == RET_OK_FUTU and not data_q.empty:
                    status = str(data_q.iloc[0].get('order_status', ''))
                    if 'FILLED' in status or 'DEALT' in status:
                        order_confirmed = True
                        logger.info(f"[平仓·确认] {position.symbol} futu_order_id={close_futu_id} 状态={status} ✓")
                        tracker.update_order_state(close_futu_id, OrderState.FILLED)
                        break
                    elif 'FAIL' in status or 'DEL' in status or 'CANCEL' in status:
                        logger.warning(f"[平仓·失败] {position.symbol} futu_order_id={close_futu_id} 状态={status} — 订单被拒绝")
                        tracker.update_order_state(close_futu_id, OrderState.FAILED, {"futu_status": status})
                        meta['_close_order_failed'] = True
                        meta['_close_order_status'] = status
                        position.metadata = meta
                        return  # 订单失败，不继续
                    else:
                        logger.debug(f"[平仓·等待] {position.symbol} 订单状态={status}, 继续等待...")

            if not order_confirmed:
                logger.warning(
                    f"[平仓·未确认] {position.symbol} futu_order_id={close_futu_id} "
                    f"8s 内未确认成交，取消 LIMIT 并降级为 MARKET 单"
                )
                # P0-fix (2026-06-15): 取消未成交 LIMIT 单，提交 MARKET 单兜底
                try:
                    from futu import ModifyOrderOp
                    scheduler.execution_engine.broker_adapter.trade_ctx.modify_order(
                        modify_order_op=ModifyOrderOp.CANCEL,
                        order_id=close_futu_id,
                        qty=0, price=0,
                        trd_env=trd_env_f,
                    )
                    logger.info(f"[平仓·降级] {position.symbol} LIMIT 单 {close_futu_id} 已取消")
                except Exception as _cancel_err:
                    logger.debug(f"[平仓·降级] 取消 LIMIT 失败 (可能已成交): {_cancel_err}")

                # 提交 MARKET 兜底单
                try:
                    _mkt_order = Order(
                        client_order_id=f"{position.symbol}_mkt_close_{int(time.time())}_{uuid.uuid4().hex[:6]}",
                        signal_id=position.signal_id,
                        symbol=position.symbol,
                        direction=Direction.SHORT if position.direction == Direction.LONG else Direction.LONG,
                        order_type=OrderType.MARKET,
                        quantity=position.size,
                        price=0,
                    )
                    _mkt_order.metadata = {'is_exit': True}
                    _mkt_result = scheduler.execution_engine.submit_order(_mkt_order)
                    _mkt_id = getattr(_mkt_result, 'futu_order_id', '') or ''
                    logger.info(f"[平仓·降级] {position.symbol} MARKET 兜底单已提交: {_mkt_id}")

                    # 快速确认 MARKET 单 (4s)
                    for _mkt_attempt in range(2):
                        _time.sleep(2)
                        _ret_mq, _data_mq = scheduler.execution_engine.broker_adapter.trade_ctx.order_list_query(
                            order_id=_mkt_id, trd_env=trd_env_f,
                        )
                        if _ret_mq == RET_OK_FUTU and not _data_mq.empty:
                            _mkt_status = str(_data_mq.iloc[0].get('order_status', ''))
                            if 'FILLED' in _mkt_status or 'DEALT' in _mkt_status:
                                order_confirmed = True
                                close_futu_id = _mkt_id
                                logger.info(f"[平仓·降级] {position.symbol} MARKET 单已成交 ✓")
                                break
                except Exception as _mkt_err:
                    logger.warning(f"[平仓·降级] {position.symbol} MARKET 兜底失败: {_mkt_err}")
                    # P0 (2026-06-16): "持仓不足" = 原 LIMIT 单已成交, 视为平仓成功
                    # 根因: 6/15 MU LIMIT@999.6 即时成交但 8s 内未检测到,
                    #        取消后 MARKET 单报"持仓不足"被误判为失败
                    _mkt_err_str = str(_mkt_err)
                    if '持仓不足' in _mkt_err_str or 'insufficient' in _mkt_err_str.lower():
                        logger.info(
                            f"[平仓·降级] {position.symbol} MARKET 报持仓不足 → "
                            f"原 LIMIT 单已成交, 视为平仓成功"
                        )
                        order_confirmed = True
                        # 使用原 LIMIT 单的 futu_id
                        close_futu_id = meta.get('close_futu_order_id', close_futu_id)

                if not order_confirmed:
                    tracker.update_order_state(close_futu_id, OrderState.STALE)
                    meta['_close_order_unconfirmed'] = True
                    meta['_close_order_submitted'] = True
                    meta['_close_futu_order_id'] = close_futu_id
                    meta['_close_exit_reason'] = exit_reason.value
                    meta['_close_pnl'] = pnl
                    meta['_close_pnl_pct'] = pnl_pct
                    meta['_close_current_price'] = current_price
                    position.metadata = meta
                    try:
                        if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
                            scheduler.trade_store.upsert_open_position(
                                position, source="scheduler", action="close_order_unconfirmed"
                            )
                    except Exception as e:
                        logger.warning(f"[平仓·未确认] DB 同步失败：{e}")
                    return
        except Exception as confirm_err:
            logger.warning(f"[平仓·确认异常] {position.symbol}: {confirm_err}")
            meta['_close_confirm_failed'] = str(confirm_err)
            position.metadata = meta
            # 确认异常，不继续执行清理和通知
            return

    # 只有确认成交后才执行后续的持仓清理和通知
    # ── Task #34 (P0, 2026-06-16): 成交确认 → 清零退避计数 ──
    try:
        _clear_close_retry(position.symbol, position.direction)
        # 同时清除 metadata 上的历史计数 (避免老记录干扰 dashboard)
        _meta_now = getattr(position, "metadata", {}) or {}
        for _k in ("_close_fail_count", "_close_last_error", "_close_last_failed_at"):
            _meta_now.pop(_k, None)
        position.metadata = _meta_now
    except Exception as _ce:
        logger.debug(f"[平仓·成交] {position.symbol} 清零退避计数异常: {_ce}")

    # 更新持仓 (标记为已平仓)
    # P0 修复 (2026-05-31): exit_time 统一用美东时间, 避免 CST 导致日期错位
    position.exit_time = now_et()
    position.exit_price = current_price
    position.exit_reason = exit_reason
    position.realized_pnl = pnl
    meta = getattr(position, "metadata", {}) or {}
    meta["_pending_close"] = True
    meta["_pending_close_at"] = now_et().isoformat()
    position.metadata = meta

    scheduler.position_store.remove(position.symbol)
    # P1 (2026-06-19): 自动平仓也清 trail_state, 与手动平仓路径对齐, 防同标的再开仓时
    # position_monitor 从旧 trail_state 恢复幽灵止损线/峰值
    try:
        from app.risk.trail_state_store import get_trail_state_store
        get_trail_state_store().delete(position.symbol)
    except Exception as _tse:
        logger.debug(f"[平仓] trail_state 清理失败 (不影响平仓): {_tse}")
    try:
        if hasattr(scheduler, 'trade_store') and scheduler.trade_store:
            scheduler.trade_store.upsert_open_position(
                position, source="scheduler", action="pending_close"
            )
    except Exception as e:
        logger.warning(f"[平仓] DB 同步失败：{e}")

    # P0-2 修复 (2026-05-19): 标 tombstone, 防止 position_sync 反向恢复刚平仓
    # 配合 app/core/position_sync.py:USPositionSync.mark_closed_tombstone (60s ttl)
    try:
        from app.core.position_sync import get_us_position_sync
        _sync = get_us_position_sync()
        _sync.mark_closed_tombstone(position.symbol)
        # P0-3 加固 (2026-05-27): 立即确认 tombstone (broker 已确认成交)
        _sync.confirm_tombstone(position.symbol)
        # ── P1 (2026-06-16): 成交后即时 sync (5s debounce) ──
        _sync.request_immediate_sync()
    except Exception as _te:
        logger.debug(f"[平仓] tombstone/sync 标记失败 (不影响平仓): {_te}")

    # Phase A (2026-05-14): 通知 coordinator 释放资金 + 锁
    try:
        _coord = getattr(scheduler, "coordinator", None)
        if _coord is not None and _coord.enabled:
            _coord.on_position_closed(
                symbol=position.symbol,
                strategy=getattr(position, "owner_strategy", None),
                position_pct=float(getattr(position, "position_pct", 0.0) or 0.0),
            )
    except Exception as _ce:
        logger.debug(f"[Coordinator] on_position_closed 失败: {_ce}")

    # 写入持久化交易记录
    try:
        meta = getattr(position, "metadata", {}) or {}
        voting_result = meta.get("voting_result", {})
        model_scores = voting_result.get("model_scores", []) if voting_result else []

        # ── P0 Fix: Fallback to trade_attribution DB when metadata is empty ──
        # trade_attribution is written at signal creation time with correct values,
        # while position.metadata may be lost due to service restart or sync overwrite.
        signal_strength = meta.get("signal_strength", 0)
        signal_confidence = meta.get("signal_confidence", 0.0)
        # Task #42 (P0, 2026-05-28): 同步从 metadata 中读 voting/decision 快照,
        # 供后面 final_strength / consensus_level 优先使用
        _decision_data = {}
        _trade_date_for_lookup = _et_trade_date(position.entry_time)
        _direction_for_lookup = (
            position.direction.value if hasattr(position.direction, "value") else str(position.direction)
        )
        if signal_strength == 0:
            signal_strength, signal_confidence = _fetch_attribution_data(
                position.trace_id,
                symbol=position.symbol,
                trade_date=_trade_date_for_lookup,
            )
            if signal_strength > 0:
                logger.info(f"[平仓·归因回填] {position.symbol} trade_date={_trade_date_for_lookup} "
                            f"signal_strength={signal_strength} confidence={signal_confidence}")
        # Task #42 (P0, 2026-05-28): trade_attribution 仍为 0 时, 从 decisions.db 反查投票结果.
        # 场景: 代码重启后 position.metadata 丢失 + position.trace_id 为空
        # (例如 close-* 兜底 trace_id) 这种双丢失的古老仓位.
        if signal_strength == 0:
            _decision_data = _fetch_decision_data(
                symbol=position.symbol,
                trade_date=_trade_date_for_lookup,
                direction=_direction_for_lookup,
            )
            if _decision_data.get("signal_strength", 0) > 0:
                signal_strength = _decision_data["signal_strength"]
                signal_confidence = _decision_data["signal_confidence"]
                logger.info(
                    f"[平仓·decisions 兜底] {position.symbol} "
                    f"trade_date={_trade_date_for_lookup} dir={_direction_for_lookup} "
                    f"decision_id={_decision_data.get('decision_id', '')} "
                    f"final_strength={signal_strength} consensus={signal_confidence}"
                )

        # P1-5 (2026-05-14): UPDATE trade_attribution 的 exit_* 字段 (F19 完整 schema)
        try:
            import sqlite3 as _sql3
            from app.core.paths import get_trade_records_db
            _holding_min = int(position.holding_hours * 60) if position.holding_hours else 0
            _exit_reason_str = exit_reason.value if hasattr(exit_reason, "value") else str(exit_reason)
            _conn = _sql3.connect(str(get_trade_records_db()))
            _conn.execute("""
                UPDATE trade_attribution SET
                    exit_time = ?, exit_price = ?, realized_pnl = ?,
                    exit_reason = ?, holding_minutes = ?, updated_at = CURRENT_TIMESTAMP
                WHERE trace_id = ?
            """, (
                position.exit_time.isoformat() if position.exit_time else None,
                float(current_price),
                float(pnl),
                _exit_reason_str,
                _holding_min,
                position.trace_id or "",
            ))
            _conn.commit()
            _conn.close()
        except Exception as _e:
            logger.debug(f"[平仓·attribution UPDATE] {position.symbol} 失败 (不影响平仓): {_e}")

        # O4 (2026-05-16): voting 缺失时回退到 signal_strength/confidence
        # 5/15 4/12 主线 trades final_strength=0 (fast-path 传 None → voting_result={} → 0)
        # Task #42 (2026-05-28): 优先顺序 — voting_result -> decisions.db -> signal_strength
        _final_strength = (voting_result or {}).get("final_strength", 0) or 0
        _consensus_level = (voting_result or {}).get("consensus_level", 0.0) or 0.0
        if not _final_strength and _decision_data.get("final_strength", 0):
            _final_strength = int(_decision_data["final_strength"])
        if not _consensus_level and _decision_data.get("consensus_level", 0.0):
            _consensus_level = float(_decision_data["consensus_level"])
        if not _final_strength:
            _final_strength = int(signal_strength or 0)
        if not _consensus_level:
            _consensus_level = float(signal_confidence or 0.0)

        # O4: 写 market_regime + mode (此前两列 NULL/DEFAULT)
        # P1-1 修复 (2026-05-23): UNKNOWN → RANGE fallback, 消除93%UNKNOWN
        _regime = ""
        try:
            _raw = position.market_regime
            if hasattr(_raw, "value"):
                _regime = _raw.value
            elif _raw:
                _regime = str(_raw)
            # 规范化: 将 legacy 非标准值映射到 MarketRegime 枚举值
            _REGIME_NORMALIZE = {
                "unknown": "RANGE", "UNKNOWN": "RANGE", "": "RANGE",
                "normal": "RANGE", "NORMAL": "RANGE", "中性": "RANGE",
                "trending": "TREND_UP", "bullish": "TREND_UP",
                "bearish": "TREND_DOWN", "high_volatility": "HIGH_VOL",
            }
            _regime = _REGIME_NORMALIZE.get(_regime, _regime) or "RANGE"
        except Exception:
            _regime = "RANGE"
        _mode = "normal"
        try:
            from app.core.config_container import get_config as _gc
            if _gc().get_dotted("mode.sandbox.enabled", False):
                _mode = "sandbox"
        except Exception as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

        # P2 修复 (2026-05-19): legacy close trace_id 兜底
        # 5/18 ARM legacy 仓位被 FLAT_ALL 强平触发的 close 事件 (id=718/719) 无 trace_id —
        # legacy 持仓没有原始 entry signal trace_id, trade_records 写空 → 下游归因/对账断链.
        # 兜底: close-side 生成 close-{symbol}-{ts} 格式 trace_id.
        _trace_id = position.trace_id
        if not _trace_id:
            _trace_id = f"close-{position.symbol}-{int(time.time() * 1000)}"
            logger.info(
                f"[平仓·trace 兜底] {position.symbol} 无 entry trace_id (legacy 仓位?), "
                f"生成 close-side: {_trace_id}"
            )
        
        # Task #15 (2026-05-27): 退出 trace_id 传递 —
        # 优先从 position.metadata.exit_trace_id (position_exit_monitor 注入) 读取,
        # 未设置则 fallback 生成 (兼容 position_monitor / FLAT_ALL 等其他退出路径).
        _exit_trace_id = (meta.get("exit_trace_id") if isinstance(meta, dict) else "") or ""
        if not _exit_trace_id:
            _exit_trace_id = (
                f"EXIT-{position.symbol}-{now_et().strftime('%Y%m%d%H%M%S')}-"
                f"{uuid.uuid4().hex[:6].upper()}"
            )
        logger.info(
            f"[EXEC][trace:{_trace_id}\u2192{_exit_trace_id}] Close order placed for {position.symbol} "
            f"(reason={exit_reason.value if hasattr(exit_reason, 'value') else exit_reason}, "
            f"qty={position.size}, pnl=${pnl:.2f})"
        )
        
        _close_trade_id = scheduler.trade_store.save_trade({
            "trace_id":          _trace_id,
            # Task #15 (2026-05-27): 持久化 exit_trace_id，与 entry trace_id 配对, 支撑全链路复盘
            "exit_trace_id":     _exit_trace_id,
            "trade_date":        _et_trade_date(position.entry_time),
            "symbol":            position.symbol,
            "direction":         position.direction.value,
            "entry_time":        position.entry_time.isoformat(),
            "exit_time":         position.exit_time.isoformat(),
            "entry_price":       position.avg_entry_price,
            "exit_price":        current_price,
            "quantity":          position.size,
            "realized_pnl":      pnl,
            "pnl_pct":           pnl_pct,
            "exit_reason":       exit_reason.value,
            "holding_hours":     position.holding_hours,
            "signal_strength":   signal_strength,
            "signal_confidence": signal_confidence,
            "futu_order_id":     getattr(position, "futu_order_id", ""),
            "close_futu_order_id": meta.get("close_futu_order_id", ""),
            "voting_result":     voting_result,
            "final_strength":    _final_strength,
            "consensus_level":   _consensus_level,
            "model_scores":      model_scores,
            "strategy_id":       meta.get("strategy_id", "") or getattr(position, "strategy_id", "main_pipeline"),  # P0 (2026-06-05): position真实值优先
            "decision_reason":   meta.get("decision_reason", ""),
            "max_drawdown":      getattr(position, 'max_adverse_excursion', 0.0) or meta.get("max_drawdown", 0.0),
            # P0 修复 (2026-05-31): signal_id 归因链路 — 从 position 读取
            "signal_id":         getattr(position, "signal_id", "") or meta.get("signal_id", "") or _trace_id,
            # Phase A (2026-05-14): 持仓的 owner_strategy 必须落到 trade_records
            "owner_strategy":    getattr(position, "owner_strategy", None) or "main",
            # [ADR-015] 归因修正 (2026-07-14): source_type 按 owner 派生, 不再漏传→默认 MAIN_PIPELINE.
            #   根因: 组合级熔断(FLAT_ALL/REDUCE_50)等主线路径平掉 playbook 仓时本 dict 漏传
            #   source_type → 通知误标"主线策略"+归因双计数 (CVE/EOG 07-14). owner=main/auto_main
            #   仍落 MAIN_PIPELINE (行为不变), 仅 playbook_*/orb_engine 归位正确来源。
            "source_type":       _OWNER_SOURCE_TYPE.get(
                                     (getattr(position, "owner_strategy", None) or "main").strip().lower(),
                                     "MAIN_PIPELINE",
                                 ),
            # O4 (2026-05-16): regime + mode 落库 (此前两列 UNKNOWN/normal 都未传入)
            "market_regime":     _regime or "RANGE",
            "mode":              _mode,
            # P0 修复 (2026-05-27): entry_reason 归因链路修复
            # 从 position.metadata 读取开仓时写入的 entry_reason, 兜底从 strategy_id 推导
            "entry_reason":      _derive_entry_reason_for_close(meta),
            # Task #14 (2026-05-27): 执行优化元数据 (TWAP / 滑点 / 延迟)
            # 2026-06-02: 附加 MFE (最大有利偏移) 用于评估止盈是否太低
            "metadata_json":     _build_close_metadata_json(meta, position),
            # P1 修复 (2026-05-31): decision_id 归因链路 — 关联 signal_snapshots + decisions
            # 优先从 position.metadata 读取 (开仓时写入), 兜底从 _decision_data 获取
            "decision_id":       meta.get("decision_id", "") or _decision_data.get("decision_id", ""),
            # 2026-06-05 数据质量修复: signal_details 写入 — 信号决策快照持久化
            "signal_details":    json.dumps({
                "signal_strength": int(signal_strength or 0),
                "signal_confidence": float(signal_confidence or 0.0),
                "voting_result": voting_result or {},
                "model_scores": model_scores or [],
                "entry_reason": _derive_entry_reason_for_close(meta),
                "regime": _regime,
                "final_strength": _final_strength,
                "consensus_level": _consensus_level,
                # P0 (2026-06-07): 因子明细落库 — 支撑因子归因分析
                "factor_detail": meta.get("factor_detail") or meta.get("_factor_detail") or {},
            }, ensure_ascii=False),
            # P0 (2026-06-07): factor_detail 独立列 — 方便 SQL 直接查询因子贡献
            "factor_detail": json.dumps(
                meta.get("factor_detail") or meta.get("_factor_detail") or {},
                ensure_ascii=False, default=str,
            ) if (meta.get("factor_detail") or meta.get("_factor_detail")) else None,
        })

        # ─── 反馈闭环修复 (2026-06-02): 平仓时回写 trade_outcome → decisions.db ───
        # 闭合 "决策→执行→结果→模型绩效" 全链路
        # link_trade_outcome 内部会:
        #   1. UPSERT trade_outcomes 表 (exit_price, realized_pnl, exit_reason, holding_hours)
        #   2. 计算 outcome_correct (1=预测正确, 0=错误, -1=未平仓)
        #   3. 触发 _update_model_performance_for_decision() 更新各模型累计绩效
        # 这是系统从 "开环执行" 变为 "可学习系统" 的关键一步
        try:
            from app.storage.decision_recorder import get_decision_recorder
            _feedback_recorder = get_decision_recorder()
            _feedback_decision_id = meta.get("decision_id", "") or _decision_data.get("decision_id", "")
            if _feedback_decision_id:
                _feedback_recorder.link_trade_outcome(
                    decision_id=_feedback_decision_id,
                    symbol=position.symbol,
                    direction=(
                        position.direction.value
                        if hasattr(position.direction, 'value')
                        else str(position.direction)
                    ),
                    entry_price=position.avg_entry_price,
                    exit_price=current_price,
                    quantity=int(position.size),
                    realized_pnl=float(pnl),
                    exit_reason=(
                        exit_reason.value
                        if hasattr(exit_reason, 'value')
                        else str(exit_reason)
                    ),
                    holding_hours=position.holding_hours,
                    entry_session=meta.get("entry_session", ""),
                    strategy_type=getattr(position, "owner_strategy", None) or "main",
                )
                logger.info(
                    f"[反馈闭环] trade_outcome 已回写: {_feedback_decision_id} "
                    f"{position.symbol} pnl=${pnl:.2f} reason={exit_reason.value}"
                )
        except Exception as _fb_e:
            logger.debug(f"[反馈闭环] trade_outcome 回写失败 (不影响平仓): {_fb_e}")
        try:
            from app.pipeline.post_trade_hook import on_position_closed as _vap_on_close
            _vap_on_close({
                "symbol": position.symbol,
                "pnl": float(pnl),
                "direction": (
                    position.direction.value
                    if hasattr(position.direction, "value")
                    else str(position.direction)
                ),
                "trade_date": _et_trade_date(position.entry_time),
            })
        except Exception as _vae:
            logger.debug(f"[投票准确率] 更新结果失败: {_vae}")

        # ─── 2026-06-05 数据质量修复: signal_lifecycle 实时写入 ───
        # 此前 signal_lifecycle 只有独立脚本 update_signal_lifecycle.py 批量同步,
        # 5/31 后无人执行导致全表停更。改为平仓时实时 INSERT。
        try:
            import sqlite3 as _sl_sql3
            from app.core.paths import get_trade_records_db as _sl_db_path
            _sl_conn = _sl_sql3.connect(str(_sl_db_path()))
            _sl_direction = position.direction.value if hasattr(position.direction, 'value') else str(position.direction)
            _sl_trade_date = _et_trade_date(position.entry_time)
            _sl_exit_reason = exit_reason.value if hasattr(exit_reason, 'value') else str(exit_reason)
            _sl_conn.execute("""
                INSERT INTO signal_lifecycle (
                    trade_id, symbol, signal_strength, signal_confidence,
                    signal_direction, trade_direction, entry_price, exit_price,
                    realized_pnl, pnl_pct, exit_reason, holding_hours,
                    owner_strategy, source_type, trade_date, trade_mode,
                    consensus_level, decision_strength, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
            """, (
                _close_trade_id,
                position.symbol,
                int(signal_strength or 0),
                float(signal_confidence or 0.0),
                _sl_direction,
                _sl_direction,
                float(position.avg_entry_price or 0),
                float(current_price or 0),
                float(pnl or 0),
                float(pnl_pct or 0),
                _sl_exit_reason,
                float(position.holding_hours or 0),
                getattr(position, "owner_strategy", None) or "main",
                "MAIN_PIPELINE",
                _sl_trade_date,
                _mode,
                float(_consensus_level or 0.0),
                int(_final_strength or 0),
                now_et().isoformat(),
            ))
            _sl_conn.commit()
            _sl_conn.close()
        except Exception as _sl_e:
            logger.debug(f"[signal_lifecycle] 写入失败 (不影响平仓): {_sl_e}")

        # Y5 (2026-05-15): PnL 异常值监控 + 标的护栏 (主线路径)
        try:
            from app.risk.symbol_daily_loss_guard import get_symbol_daily_loss_guard
            get_symbol_daily_loss_guard().record_trade(position.symbol, float(pnl))
        except Exception as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

        # C4 fix (2026-05-31): 主线 P&L 计入共享日回撤池
        try:
            _risk_eng = getattr(scheduler, "risk_engine", None)
            _dd_ctrl = getattr(_risk_eng, "drawdown_controller", None) if _risk_eng else None
            if _dd_ctrl is not None:
                _dd_ctrl.record_trade_pnl(float(pnl), source="main")
        except Exception as _dd_e:
            logger.debug(f"[主线·C4] 回撤上报失败 (不影响平仓): {_dd_e}")

        # REMOVED (2026-07-31, DEB-feedback-loop 收敛项5): Task #29 教训应用验证块 —
        #   LessonInjector.verify_lesson_application 全仓无定义, 调用必 raise 被 except 吞,
        #   自始零产出 (死调用). 删除为代码卫生, 零行为变更.
        # REMOVED (2026-06-19): ShadowMirror 不再使用
        # 改用 Playbook shadow 模式做策略验证
        try:
            from app.risk.pnl_outlier_monitor import get_pnl_outlier_monitor
            get_pnl_outlier_monitor().check(position.symbol, float(pnl), {
                "direction": str(position.direction.value if hasattr(position.direction, "value") else position.direction),
                "source": "main",
                "exit_reason": str(exit_reason.value if hasattr(exit_reason, "value") else exit_reason),
            })
        except Exception as _silent_e:
            logger.debug(f"[silent_except:order_executor.py] {_silent_e}")

        # ── P0 Fix (2026-05-02): 关联决策结果到 decisions.db trade_outcomes ──
        # 这是 model_performance 更新和方向识别率评估的关键链路
        try:
            from app.storage.decision_recorder import get_decision_recorder
            recorder = get_decision_recorder()

            # 尝试从 metadata 获取 decision_id
            decision_id = meta.get("decision_id", "")
            if not decision_id:
                # Fallback: 通过 trace_id 从 trade_attribution 反查 decision_id
                if position.trace_id:
                    decision_id = _lookup_decision_id_by_trace(position.trace_id, position.symbol)

            if decision_id:
                recorder.link_trade_outcome(
                    decision_id=decision_id,
                    symbol=position.symbol,
                    direction=position.direction.value,
                    entry_price=position.avg_entry_price,
                    quantity=position.size,
                    exit_price=current_price,
                    realized_pnl=pnl,
                    exit_reason=exit_reason.value,
                    holding_hours=position.holding_hours,
                    entry_session=meta.get("entry_session"),
                    strategy_type=meta.get("strategy_type", "") or meta.get("strategy_id", "") or getattr(position, "strategy_id", "main_pipeline"),  # P0 (2026-06-05)
                )
                logger.info(
                    f"[平仓·决策关联] {position.symbol} decision_id={decision_id} "
                    f"exit=${current_price:.2f} pnl=${pnl:.2f} reason={exit_reason.value}"
                )
            else:
                # P0 修复 (F20 2026-05-12): 不能丢数据, 写入 orphan_outcomes 用于后续手动归因
                logger.warning(
                    f"[平仓·决策关联] {position.symbol} 无法找到 decision_id "
                    f"(trace_id={position.trace_id})，写入 orphan_outcomes"
                )
                try:
                    from app.core.db_utils import get_main_db
                    import sqlite3 as _sq
                    orphan_db = get_main_db()
                    oconn = _sq.connect(str(orphan_db))
                    oconn.execute("""CREATE TABLE IF NOT EXISTS orphan_outcomes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        symbol TEXT, direction TEXT, exit_price REAL, realized_pnl REAL,
                        exit_reason TEXT, exit_time TEXT, trace_id TEXT, size INTEGER,
                        created_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )""")
                    oconn.execute(
                        "INSERT INTO orphan_outcomes (symbol, direction, exit_price, realized_pnl, exit_reason, exit_time, trace_id, size) VALUES (?,?,?,?,?,?,?,?)",
                        (position.symbol, position.direction.value, current_price, pnl, exit_reason.value, None, position.trace_id, position.size)
                    )
                    oconn.commit()
                    oconn.close()
                except Exception as _e:
                    logger.error(f"[orphan_outcomes] 写入失败: {_e}")
        except Exception as e:
            logger.warning(f"[平仓·决策关联] 失败 (不影响平仓主流程): {e}")

        # ── Phase 2: 更新归因记录 (正常流程) ──
        _update_trade_attribution(
            trace_id=position.trace_id,
            symbol=position.symbol,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=exit_reason.value,
            holding_hours=position.holding_hours,
            signal_strength=signal_strength,
            signal_confidence=signal_confidence,
            max_drawdown=meta.get("max_drawdown", 0.0),
        )

        # ═══════════════════════════════════════════════════════════
        # P0 修复 (2026-05-08): 写入 signal_outcomes 表
        # 打通主管线 (MAIN_PIPELINE) 学习闭环 — 552 笔交易中 96 笔主管线缺失信号结果
        # ═══════════════════════════════════════════════════════════
        try:
            import sqlite3
            from app.core.paths import get_trade_records_db
            db_path = str(get_trade_records_db())

            # 从 meta 提取策略上下文
            strategy_id = meta.get("strategy_id", "") or getattr(position, "strategy_id", "main_pipeline")  # P0 (2026-06-05)
            market_regime = meta.get("market_regime", "RANGE")
            max_dd = meta.get("max_drawdown", 0.0)

            # 映射 exit_reason 到 regime (fallback)
            # P1-1 (2026-05-23): 使用标准 MarketRegime 枚举值
            if market_regime in ("UNKNOWN", "unknown", ""):
                er = exit_reason.value.lower()
                if "trail" in er:
                    market_regime = "TREND_UP"
                elif "stop" in er or "flash" in er:
                    market_regime = "HIGH_VOL"
                else:
                    market_regime = "RANGE"

            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """INSERT INTO signal_outcomes
                       (code, action, strategy, entry_price, exit_price, pnl_pct,
                        holding_periods, market_regime, scenario, adx_value,
                        mtf_alignment, sentiment_score, consensus_score,
                        quality_score, max_drawdown, max_profit, confidence, signal_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        position.symbol,
                        position.direction.value,
                        strategy_id,
                        position.avg_entry_price,
                        current_price,
                        pnl_pct * 100,  # 存储百分比
                        max(1, int(position.holding_hours * 4)) if position.holding_hours else 1,
                        market_regime,
                        meta.get("lifecycle", meta.get("entry_session", "unknown")),
                        meta.get("adx", 0.0) or 0.0,
                        meta.get("mtf_alignment", 0.0) or 0.0,
                        meta.get("sentiment_score", 0.0) or 0.0,
                        signal_confidence if signal_confidence > 0 else 0.5,
                        1.0,
                        abs(max_dd) if max_dd and max_dd < 0 else 0.0,
                        abs(pnl_pct) if pnl_pct > 0 else 0.0,
                        signal_confidence if signal_confidence > 0 else 0.5,
                        position.signal_id,  # P0-2 (2026-05-20): 信号-交易关联
                    )
                )
                conn.commit()

            logger.info(
                f"✅ [信号结果] {position.symbol} MAIN_PIPELINE 已写入 signal_outcomes | "
                f"strategy={strategy_id} | pnl={pnl_pct*100:+.2f}% | "
                f"regime={market_regime} | exit={exit_reason.value}"
            )
        except Exception as outcome_e:
            logger.warning(f"[信号结果] MAIN_PIPELINE 写入 signal_outcomes 失败 (不影响主流程): {outcome_e}")
        # ═══════════════════════════════════════════════════════════

        # ── Phase 3: 记录退出事件 (WAL) ──
        # 2026-07-26 减法R2: event_log 表全仓零 SELECT 读者, import 即起常驻线程空耗, 停写。
        # 退出记录真源 = 上方 signal_outcomes 写入。回滚=恢复 log_event(EXIT) 调用块。

    except Exception as e:
        logger.warning(f"[平仓] 写入交易记录失败（不影响平仓）: {e}")

    # 记录事件
    scheduler.event_store.append(TradeEvent(
        event_type="position_closed",
        position_id=position.position_id,
        signal_id=position.signal_id,
        data={
            "symbol": position.symbol,
            "exit_reason": exit_reason.value,
            "exit_price": current_price,
            "entry_price": position.avg_entry_price,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "holding_hours": position.holding_hours,
        },
        source="scheduler",
        severity=AlertLevel.INFO,
    ))

    # 推送平仓通知
    try:
        _notify_exit(scheduler, position, exit_reason, pnl, pnl_pct)
    except Exception as e:
        logger.debug(f"[平仓] 通知失败: {e}")

    # Phase 1: Obsidian 双写 — 美股平仓交易记录
    try:
        from app.core.obsidian_writer import get_obsidian_writer

        writer = get_obsidian_writer()
        direction_str = position.direction.value if hasattr(position.direction, 'value') else str(position.direction)
        writer.write_trade({
            'id': f"US-{position.symbol.replace('.', '_')}-{int(time.time())}",
            'code': f"US.{position.symbol}",
            'market': 'US',
            'entry_time': position.entry_time.isoformat() if hasattr(position.entry_time, 'isoformat') else str(position.entry_time),
            'exit_time': position.exit_time.isoformat() if hasattr(position.exit_time, 'isoformat') else str(position.exit_time),
            'entry_price': position.avg_entry_price,
            'exit_price': current_price,
            'quantity': position.size,
            'pnl': pnl,
            'pnl_pct': pnl_pct,
            'strategy': meta.get('strategy_id', '') or getattr(position, 'strategy_id', 'main_pipeline'),  # P0 (2026-06-05)
            'action': direction_str.upper(),
            'exit_reason': exit_reason.value if hasattr(exit_reason, 'value') else str(exit_reason),
            'tags': ['us_market', exit_reason.value if hasattr(exit_reason, 'value') else 'exit'],
            'stop_loss': getattr(position, 'stop_loss', None),
            'take_profit': getattr(position, 'take_profit', None),
            'highest_price': meta.get('highest_price', None),
            'lowest_price': meta.get('lowest_price', None),
        })
        logger.debug(f"[Obsidian] 美股平仓已写入: {position.symbol}")
    except Exception as _e:
        logger.debug(f"[Obsidian] 美股平仓写入失败: {_e}")

    # 风控：连续亏损记录
    if pnl < 0 and scheduler.risk_engine and hasattr(scheduler.risk_engine, "strategy_breaker"):
        try:
            _strat = getattr(position, 'strategy_id', '') or getattr(position, 'owner_strategy', '') or "main_pipeline"
            scheduler.risk_engine.strategy_breaker.record_loss(_strat)  # P0 (2026-06-05): 用真实策略名
        except Exception as e:
            logger.debug(f"[平仓] 策略熔断记录失败: {e}")