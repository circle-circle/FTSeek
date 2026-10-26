"""实盘写路径服务端闸门 (RFC-168 方案 A/B, 2026-10-26)

背景 (2026-10-26 全系统审计):
  Dashboard 有 5 条实盘写路径绕过统一闸门。其中 runtime.capabilities 的
  "断线只读态" **只被前端消费** (runtime_status.py:14 自述"前端提示层") ⇒
  券商断线 / 持仓降级时直接 POST /api/orders 仍会成交 ——
  UI 锁不是安全锁。

设计原则 (RFC-168 §提议方案 A):
  **统一的是「校验范式」, 不是「同一套闸门」。**
  收紧只作用在**开新仓**与**降低保护**两个方向;
  平仓 / 撤单 / 改善止损三个方向必须保持可达 ——
  给平仓套上开仓的金额上限, 会制造"更安全但更危险"的系统
  (该拒绝减仓的时候拒绝减仓, 才是真正的危险)。

开关 (一行回滚; 不改 yaml 以避开热重载 60s 延迟窗口):
  FTSEEK_DASHBOARD_WRITE_GATES=off      → 全部闸关闭
  FTSEEK_DASHBOARD_GATE_<NAME>=off      → 单闸关闭 (NAME ∈ PLACE/CANCEL/
                                          MODIFY_STOP/PROTECTION_CONFIRM/HK_PLACE)

边界:
  - 不改四锁、不改仓位/止损止盈/频率、不改信号分布。
  - 所有闸只在**写**路径上生效, 不影响只读快照与报表。
  - 拒绝时走 app/push/ hub 告警 (军规: 新增推送必须走 hub), 静默门控 —
    仅在拒绝真的发生时推 (状态变化才推, 符合"无料不推")。
"""
from __future__ import annotations

import logging
import os
from typing import Any, Tuple

logger = logging.getLogger("ftseek_us.web_dashboard.write_gates")

_OFF_VALUES = {"off", "0", "false", "no", "disabled"}


def gate_enabled(name: str) -> bool:
    """单个闸的开关状态 (默认全开)。"""
    if str(os.environ.get("FTSEEK_DASHBOARD_WRITE_GATES", "")).strip().lower() in _OFF_VALUES:
        return False
    env_key = f"FTSEEK_DASHBOARD_GATE_{name.strip().upper()}"
    return str(os.environ.get(env_key, "")).strip().lower() not in _OFF_VALUES


def _notify_denial(kind: str, detail: str) -> None:
    """拒绝事件推 hub (走「状态变化才推」: 拒绝本身就是状态变化)。"""
    try:
        from app.push import get_push_hub, PushMessage, Level, Category
        get_push_hub().push(PushMessage(
            title=f"🚫 写路径闸门拦截 ({kind})",
            body=detail,
            level=Level.WARNING,
            category=Category.RISK_ALERT,
            dedup_key=f"write_gate_denied:{kind}:{detail[:80]}",
        ))
    except Exception as _pe:
        logger.warning(f"[write_gates] 拒绝告警发送失败 ({kind}): {_pe}", exc_info=True)


def _deny(kind: str, detail: str) -> Tuple[bool, str]:
    logger.warning(f"[write_gates] 拦截 ({kind}): {detail}", exc_info=True)
    _notify_denial(kind, detail)
    return False, detail


def _allow_unverified(kind: str, detail: str) -> Tuple[bool, str]:
    """归属/状态**不可判定**时放行 (仅用于保护与减仓方向)。

    方向性原则 (RFC-168 §提议方案 A):
      - **开仓**方向读不到权威状态 → fail-close (宁可不下单);
      - **平仓 / 撤单 / 改善止损**方向读不到台账 → fail-open (该拒绝减仓的
        时候拒绝减仓, 才是真正的危险)。
    本函数只用于后者, 且必带告警 —— "判据不可用"必须留痕。
    """
    logger.warning(f"[write_gates] 放行但状态不可判定 ({kind}): {detail}", exc_info=True)
    try:
        from app.push import get_push_hub, PushMessage, Level, Category
        get_push_hub().push(PushMessage(
            title=f"⚠️ 写路径判据不可判定 ({kind})",
            body=detail,
            level=Level.WARNING,
            category=Category.RISK_ALERT,
            dedup_key=f"write_gate_unverified:{kind}",
        ))
    except Exception as _pe:
        logger.warning(f"[write_gates] 状态不可判定告警发送失败 ({kind}): {_pe}", exc_info=True)
    return True, ""


# ─────────────────────────────────────────────────────────────
#  台账读取 (看板手动实盘台 — 一律 get_dashboard_db, 恒 live 库)
# ─────────────────────────────────────────────────────────────

def _position_row(service: Any, symbol: str):
    """从持仓台账取 (direction, size, current_sl, avg_entry_price)。

    返回 (row, readable):
      row 非 None → 命中该标的;
      row None + readable True → 查询成功但无此标的 (真正的归属违规);
      row None + readable False → **查询失败** (判据不可用, 走 fail-open 分支)。
    三态不可合并: 把"读不到"当"不存在"会让改止损这类**保护动作**被无端拒绝。
    """
    conn = None
    try:
        conn = service._db()
        cur = conn.execute(
            "SELECT direction, size, current_sl, avg_entry_price "
            "FROM open_positions WHERE symbol=?",
            (symbol,),
        )
        return cur.fetchone(), True
    except Exception as e:
        logger.warning(f"[write_gates] 持仓台账查询失败 ({symbol}): {e}", exc_info=True)
        return None, False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as _ce:
                logger.warning(f"[write_gates] 台账连接关闭异常: {_ce}", exc_info=True)


# ─────────────────────────────────────────────────────────────
#  归属校验
# ─────────────────────────────────────────────────────────────

def check_symbol_held(service: Any, symbol: str) -> Tuple[bool, str]:
    """标的须在本账户持仓台账内 (归属校验)。

    只查本地台账, 不做 broker RPC —— 台账是"本看板在管哪些仓位"的权威。
    台账读不到时 fail-open (保护方向不该因判据不可用而被拦), 但必告警。
    """
    row, readable = _position_row(service, symbol)
    if row:
        return True, ""
    if not readable:
        return _allow_unverified(
            "symbol_held_unverified",
            f"持仓台账不可读, 无法确认 {symbol} 归属; 本次操作放行 (保护方向不因判据不可用被阻断)",
        )
    return _deny("symbol_not_held",
                 f"{symbol} 不在本账户持仓台账, 拒绝该操作")


def check_cancel_ownership(service: Any, order_id: str) -> Tuple[bool, str]:
    """撤单归属校验: order_id 必须属本账户。

    两级证据: ①本地手动审计台账命中 (快路径, 无 RPC);
    ②否则查 broker 订单列表确认该单确属本账户。
    两者皆不命中 → 拒绝 (持合法会话即可撤任意单是真实风险面)。
    broker 查询失败 → fail-open: 此时 cancel 本身的 RPC 同样会失败, 无实质风险,
    且撤单属减风险方向, 不该因判据不可用被拦 (但必告警)。
    """
    conn = None
    try:
        conn = service._db()
        cur = conn.execute(
            "SELECT order_id FROM manual_order_audit WHERE order_id=? LIMIT 1",
            (order_id,),
        )
        if cur.fetchone():
            return True, ""
    except Exception as e:
        logger.warning(f"[write_gates] 审计台账查询失败 ({order_id}): {e}", exc_info=True)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception as _ce:
                logger.warning(f"[write_gates] 台账连接关闭异常: {_ce}", exc_info=True)

    try:
        from futu import RET_OK
        ret, data = service._broker_ctx.order_list_query(
            order_id=order_id, trd_env=service._trd_env(), acc_id=service._acc_id(),
        )
        if ret == RET_OK and data is not None and not data.empty:
            return True, ""
        if ret != RET_OK:
            return _allow_unverified(
                "cancel_ownership_unverified",
                f"无法确认订单 {order_id} 归属本账户 (broker 查询失败), 放行撤单",
            )
        logger.info(f"[write_gates] broker 未查到订单 {order_id} (ret={ret})")
    except Exception as e:
        logger.warning(f"[write_gates] broker 订单归属查询异常 ({order_id}): {e}", exc_info=True)
        return _allow_unverified(
            "cancel_ownership_unverified",
            f"无法确认订单 {order_id} 归属本账户 ({e}), 放行撤单",
        )

    return _deny("cancel_not_owned",
                 f"订单 {order_id} 不属于本账户, 拒绝撤销")


# ─────────────────────────────────────────────────────────────
#  止损方向校验 (只拦「降低保护」)
# ─────────────────────────────────────────────────────────────

def check_stop_loss_direction(
    service: Any,
    symbol: str,
    new_sl: float,
    acknowledge: bool = False,
) -> Tuple[bool, str]:
    """改止损方向校验。

    只拦**降低保护**的方向, 放行**增强保护**的方向:
      LONG  止损在入场价下方 → SL 上移 = 收紧保护, SL 下移 = 降低保护;
      SHORT 止损在入场价上方 → SL 下移 = 收紧保护, SL 上移 = 降低保护。
    另: SL 越过入场价 = 触发即亏损离场 = 极端的降低保护。

    acknowledge=True 表示调用方已取得用户显式确认 → 放行 (不是硬拒绝,
    因为"放宽止损"常是用户主动规避噪音扫出的正当诉求, 不该被一刀切禁掉)。
    """
    row, readable = _position_row(service, symbol)
    if row is None:
        if not readable:
            return _allow_unverified(
                "stop_direction_unverified",
                f"持仓台账不可读, 无法判定 {symbol} 止损方向; 本次操作放行 (保护方向不因判据不可用被阻断)",
            )
        return _deny("symbol_not_held",
                     f"{symbol} 不在本账户持仓台账, 拒绝修改止损")

    direction = str(row[0] or "").strip().upper()
    cur_sl = row[2]
    entry = row[3]

    if direction not in ("LONG", "SHORT"):
        logger.info(f"[write_gates] {symbol} 持仓方向未知 ({direction!r}) — 方向校验跳过")
        return True, ""

    def _num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    cur_sl_num = _num(cur_sl)
    entry_num = _num(entry)
    weakens = False
    reason = ""

    if direction == "LONG":
        if cur_sl_num is not None and new_sl < cur_sl_num:
            weakens = True
            reason = f"多头止损由 ${cur_sl_num:.2f} 下移至 ${new_sl:.2f} (放宽保护)"
        if entry_num is not None and new_sl >= entry_num:
            weakens = True
            reason = f"多头止损 ${new_sl:.2f} 已越过入场价 ${entry_num:.2f} (触发即亏损离场)"
    else:  # SHORT
        if cur_sl_num is not None and new_sl > cur_sl_num:
            weakens = True
            reason = f"空头止损由 ${cur_sl_num:.2f} 上移至 ${new_sl:.2f} (放宽保护)"
        if entry_num is not None and new_sl <= entry_num:
            weakens = True
            reason = f"空头止损 ${new_sl:.2f} 已越过入场价 ${entry_num:.2f} (触发即亏损离场)"

    if not weakens:
        return True, ""
    if acknowledge:
        logger.info(f"[write_gates] {symbol} 降低止损保护已获用户显式确认: {reason}")
        return True, ""
    return _deny("stop_loss_weakened",
                 f"{symbol} {reason}。该操作须用户显式确认后方可执行")


# ─────────────────────────────────────────────────────────────
#  开仓前置 (runtime.capabilities 的服务端等价物)
# ─────────────────────────────────────────────────────────────

def check_account_known(service: Any) -> Tuple[bool, str]:
    """账户资金须可实时确认 (runtime 的 account_known 的服务端等价物)。"""
    try:
        from futu import RET_OK
        ret, acc = service._broker_ctx.accinfo_query(
            trd_env=service._trd_env(), acc_id=service._acc_id(),
        )
        if ret != RET_OK or acc is None or acc.empty:
            return _deny("account_unknown",
                         "账户资金不可用 (券商未返回), 已阻止开仓")
    except Exception as e:
        return _deny("account_query_failed",
                     f"账户资金查询异常 ({e}), 已阻止开仓")
    return True, ""


def check_positions_authoritative(service: Any) -> Tuple[bool, str]:
    """当前持仓须可实时确认 (runtime 的 positions_state=='live' 的服务端等价物)。

    查不到 = 无法确认当前持仓状态 ⇒ 不能保证不重复开仓 ⇒ fail-close。
    """
    try:
        from futu import RET_OK
        ret, _data = service._broker_ctx.position_list_query(
            trd_env=service._trd_env(), acc_id=service._acc_id(), refresh_cache=True,
        )
        if ret != RET_OK:
            return _deny("positions_not_authoritative",
                         "券商持仓查询失败, 无法确认当前持仓状态, 已阻止开仓")
    except Exception as e:
        return _deny("positions_query_failed",
                     f"券商持仓查询异常 ({e}), 已阻止开仓")
    return True, ""


def precheck_place(service: Any) -> Tuple[bool, str]:
    """开新仓前置校验 (仅开仓侧收紧; 平仓/撤单/改善止损不受此约束)。"""
    if not gate_enabled("PLACE"):
        return True, ""
    ok, msg = check_account_known(service)
    if not ok:
        return False, msg
    ok, msg = check_positions_authoritative(service)
    if not ok:
        return False, msg
    return True, ""


def precheck_cancel(service: Any, order_id: str) -> Tuple[bool, str]:
    """撤单前置校验 (归属)。"""
    if not gate_enabled("CANCEL"):
        return True, ""
    return check_cancel_ownership(service, order_id)


def precheck_modify_stop(
    service: Any,
    symbol: str,
    new_sl: float,
    acknowledge: bool = False,
) -> Tuple[bool, str]:
    """改止损前置校验 (归属 + 方向)。"""
    if not gate_enabled("MODIFY_STOP"):
        return True, ""
    ok, msg = check_symbol_held(service, symbol)
    if not ok:
        return False, msg
    return check_stop_loss_direction(service, symbol, new_sl, acknowledge=acknowledge)


def precheck_protection_confirm(service: Any, symbol: str) -> Tuple[bool, str]:
    """保护确认前置校验 (归属)。"""
    if not gate_enabled("PROTECTION_CONFIRM"):
        return True, ""
    return check_symbol_held(service, symbol)


# ─────────────────────────────────────────────────────────────
#  港股下单 (缺 kill switch / 资本水位 / 回撤熔断)
# ─────────────────────────────────────────────────────────────

def precheck_hk_place() -> Tuple[bool, str]:
    """港股开仓前置校验。

    hk_order_service.validate() 只做格式/整手/当日单数/金额/价格偏离,
    **无 kill switch / 无资本水位 / 无回撤熔断** ⇒ 港股绕过了美股 validator
    的全部全局闸。此处补齐开仓侧的三条全局闸 (与美股口径一致)。
    """
    if not gate_enabled("HK_PLACE"):
        return True, ""

    try:
        from app.us_pipeline.emergency_interceptor import check_kill_switch
        if check_kill_switch():
            return _deny("hk_kill_switch", "🚨 紧急停止开关已激活, 港股下单被拦截")
    except Exception as e:
        logger.warning(f"[write_gates] 港股 Kill Switch 检查异常: {e}", exc_info=True)

    try:
        from app.risk.capital_safety import get_capital_safety_system, WaterLevel
        cs = get_capital_safety_system()
        ok, msg = cs.can_open_position()
        if not ok:
            wl = getattr(cs._state, "water_level", None)
            if wl == WaterLevel.BLACKOUT:
                return _deny("hk_capital_blackout", f"🚨 资本安全 BLACKOUT 熔断: {msg}")
            logger.warning(f"[write_gates] 港股下单: 资本安全 {wl} 警告 (不阻断): {msg}", exc_info=True)
    except Exception as e:
        logger.warning(f"[write_gates] 港股资本安全检查异常: {e}", exc_info=True)

    try:
        import importlib
        sc_mod = importlib.import_module("app.core.scheduler_core")
        sched = getattr(sc_mod, "_scheduler_instance", None)
        dd = getattr(getattr(sched, "risk_engine", None), "drawdown_controller", None) if sched else None
        if dd is not None and not dd.is_trading_allowed():
            return _deny("hk_drawdown", f"回撤熔断: {dd.get_block_reason() or '日回撤超限'}")
    except Exception as e:
        logger.warning(f"[write_gates] 港股回撤检查异常: {e}", exc_info=True)

    return True, ""
