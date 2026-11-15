"""S1 臂闸 (判据字典 B23 / 注册表 s1_short_after_open) — 单一真源。

实验授权 (用户 2026-11-15 裁): **只做空 / 只在 ET 09:45 之后 / 单笔 ≤$4,200 /
单日 ≤2 笔 / 总敞口 ≤$8,400**, 累计净 −$300 或前 15 笔净为负即停 (kill switch)。

纪律:
  · `s1_arm_enabled=false` (默认) 时 `cycle_state()` 返回 enabled=False, 调用方一行都不拦
    ⇒ 零行为变更, 回滚 = 把该键改回 false (热载 ~60s, 无需重启亦可)。
  · 本模块只读 DB (`mode=ro`), 不下单、不写交易库、不改任何指令字段。
  · 帽的判据全部来自判据字典 B23 原文, 改阈值 = 重新裁决, 不得在此处"微调"。

调用约定 (每个 auto_execute 轮次一次 cycle_state, 每条指令一次 check_size/check_entry):
    st = cycle_state(today_et_str, today_opened_count)
    ok, reason = check_entry(st, direction='SELL_SHORT', entry_minute=_now_min)
    size = cap_size(st, size, entry_price)   # 超帽时按帽缩股 (整股向下取整)
"""
from __future__ import annotations

import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_DB = Path(__file__).resolve().parents[2] / "data" / "ftseek_us.db"

DEFAULTS = {
    "s1_arm_enabled": False,
    "s1_arm_start_date": "2026-11-16",
    "s1_arm_allowed_direction": "SELL_SHORT",
    "s1_arm_min_entry_minute_et": 585,      # 09:45 ET
    "s1_arm_single_notional_cap_usd": 4200.0,
    "s1_arm_max_entries_per_day": 2,
    "s1_arm_max_open_exposure_usd": 8400.0,
    "s1_arm_kill_cum_pnl_usd": -300.0,
    "s1_arm_kill_after_n": 15,
}

# 判据 B23 的 S1 人群 (与 scripts/s1_short_after_open_cohort.py 同一谓词; 改动须同步两处)
S1_WHERE = (
    "exit_time IS NOT NULL AND pnl_net IS NOT NULL "
    "AND trade_date >= '2026-07-01' "
    "AND COALESCE(data_quality,'') IN ('clean','real') "
    "AND COALESCE(source_type,'') NOT IN ('POSITION_SYNC','MANUAL') "
    "AND direction = 'SHORT'"
)


def _cfg(key: str):
    """读 playbook.execution_parameters 拍平键; 读不到返回 None (调用方回落 DEFAULTS)。"""
    try:
        from app.playbook.config import PlaybookConfig

        return PlaybookConfig().get(key)
    except Exception:
        return None


def cfg(key: str):
    v = _cfg(key)
    return DEFAULTS[key] if v is None else v


def enabled() -> bool:
    v = cfg("s1_arm_enabled")
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def _connect():
    conn = sqlite3.connect("file:%s?mode=ro" % _DB, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _et_minute(ts: str) -> Optional[int]:
    """entry_time → ET 当日分钟数 (库内混存多偏移, 禁写死 −4h)。"""
    if not ts:
        return None
    try:
        from app.utils.timezone import parse_iso_safe, to_et

        dt = parse_iso_safe(ts)
        if dt is None:
            return None
        et = to_et(dt)
        return et.hour * 60 + et.minute
    except Exception:
        return None


def open_exposure_usd(start_date: Optional[str] = None) -> float:
    """本实验的在仓名义额 (trade_records 未平仓行; 平仓即释放)。

    只计 `trade_date >= start_date` 的仓: 臂启动前遗留的旧仓 (SIM 期) 不计入帽,
    否则历史在仓会把新实验永久卡在敞口帽上 (实测 11-15 遗留在仓 $33.8k)。
    失败返回 0.0 (不阻断交易)。"""
    if start_date is None:
        start_date = str(cfg("s1_arm_start_date"))
    try:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(ABS(quantity * entry_price)), 0) FROM trade_records"
                " WHERE exit_time IS NULL AND trade_date >= ?",
                (start_date,),
            ).fetchone()
            return float(row[0] or 0.0)
        finally:
            conn.close()
    except Exception:
        return 0.0


def cohort_state(start_date: str) -> Dict[str, Any]:
    """前瞻 cohort 的已平仓读数 (kill switch 与判据共用口径)。"""
    out = {"n": 0, "net": 0.0, "net_bps": None, "fee_bps": None}
    try:
        conn = _connect()
        try:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT entry_time, exit_time, quantity, entry_price, realized_pnl,"
                    " fees_est, pnl_net FROM trade_records"
                    " WHERE " + S1_WHERE + " AND trade_date >= ?",
                    (start_date,),
                )
            ]
        finally:
            conn.close()
    except Exception:
        return out
    keep = [r for r in rows if (_et_minute(r["entry_time"]) or 0) >= int(cfg("s1_arm_min_entry_minute_et"))]
    if not keep:
        return out
    no = sum((r["quantity"] or 0) * (r["entry_price"] or 0) for r in keep)
    net = sum(r["pnl_net"] for r in keep)
    fee = sum(r["fees_est"] or 0 for r in keep)
    out.update(
        n=len(keep),
        net=round(net, 2),
        net_bps=round(net / no * 1e4, 2) if no else None,
        fee_bps=round(fee / no * 1e4, 2) if no else None,
    )
    return out


def kill_reason(state: Dict[str, Any]) -> Optional[str]:
    """S1 方向性 / S2 经济性 两条停止条件 (B23 原文)。"""
    n = state.get("n") or 0
    if state.get("net", 0.0) <= float(cfg("s1_arm_kill_cum_pnl_usd")):
        return "S2_ECON(cum_net=%.2f)" % state.get("net", 0.0)
    if n >= int(cfg("s1_arm_kill_after_n")) and (state.get("net_bps") or 0) < 0:
        return "S1_DIR(n=%d,net_bps=%.2f)" % (n, state.get("net_bps") or 0.0)
    return None


def cycle_state(today: str, today_opened_count: int) -> Dict[str, Any]:
    """每轮评估一次; enabled=False 时所有字段留空且不查库。"""
    st: Dict[str, Any] = {
        "enabled": False, "today": today, "today_count": int(today_opened_count or 0),
        "exposure": 0.0, "killed": None, "cohort": {"n": 0, "net": 0.0},
    }
    is_on = enabled()
    if not is_on:
        return st
    st["enabled"] = is_on
    st["exposure"] = open_exposure_usd()
    st["cohort"] = cohort_state(str(cfg("s1_arm_start_date")))
    st["killed"] = kill_reason(st["cohort"])
    return st


def check_entry(st: Dict[str, Any], direction: str, entry_minute: Optional[int]) -> Tuple[bool, str]:
    """方向/时段/日笔数/敞口/kill switch 五道。返回 (放行?, 拒绝原因)。"""
    if not st.get("enabled"):
        return True, ""
    if st.get("killed"):
        return False, "s1_arm_killed:%s" % st["killed"]
    allowed = str(cfg("s1_arm_allowed_direction")).upper()
    if str(direction or "").upper() != allowed:
        return False, "s1_arm_direction(%s!=%s)" % (direction, allowed)
    if entry_minute is None or int(entry_minute) < int(cfg("s1_arm_min_entry_minute_et")):
        return False, "s1_arm_before_open(%s)" % entry_minute
    cap_n = int(cfg("s1_arm_max_entries_per_day"))
    if st.get("today_count", 0) >= cap_n:
        return False, "s1_arm_daily_count(%d>=%d)" % (st.get("today_count", 0), cap_n)
    cap_x = float(cfg("s1_arm_max_open_exposure_usd"))
    if (st.get("exposure") or 0.0) >= cap_x:
        return False, "s1_arm_exposure(%.0f>=%.0f)" % (st.get("exposure") or 0.0, cap_x)
    return True, ""


def cap_size(st: Dict[str, Any], size: int, entry_price: float) -> int:
    """按单笔名义帽缩股 (整股向下取整); 不放大。enabled=False 原样返回。"""
    if not st.get("enabled") or not size or entry_price <= 0:
        return size
    cap_usd = float(cfg("s1_arm_single_notional_cap_usd"))
    room = min(cap_usd, max(0.0, float(cfg("s1_arm_max_open_exposure_usd")) - (st.get("exposure") or 0.0)))
    max_qty = int(math.floor(room / entry_price))
    return int(min(size, max_qty))