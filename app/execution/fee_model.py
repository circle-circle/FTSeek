"""富途美股费用模型 (DEB-20260730-fee-net-pnl D2, 用户裁"执行" 2026-07-30)。

纯函数、零 IO(除一次性读配置)、可单测 — 按用户提供的富途美股费率表估算每订单费用:
  佣金        $0.01/股, min $1.99/单, max 0.3%×成交金额   双边
  平台使用费   $0                                          —
  交收费      $0.003/股                                    双边
  SEC 规费    rate×成交金额, min $0.01                     仅卖出 (含卖空开仓腿, §31 惯例)
  TAF        $0.000195/股, min $0.01, max $9.79           仅卖出 (同上)
  CAT        NMS $0.000003/股; OTC $0.00000003/股         双边, 无 min

关键口径 (辩论收敛, DEB §九/§十):
  - 佣金优先级: max(min(per_share×qty, 0.003×amt), 1.99) — 先 cap 后 floor
  - SEC 费率是 effective_from 时间序列 (官网实证: 2026-04-04 前 $0, 之后 $20.60/M) —
    回填历史单必须按成交日选费率, 单值会多收
  - 借券费: 仅隔夜 SHORT 计提 (日内=0); 费率来自 get_margin_ratio 当日快照, 历史近似须打标
  - est ≠ 实收: SIM 期唯一可行口径 (order_fee_query SIM 实测返空); LIVE 后升级 broker/reconciled

费率表可被 unified_config.yaml `broker.futu_us` 节覆盖 (券商调价=改配置留 ConfigGov 痕),
读不到配置时用内置缺省 — 保证纯函数在任何环境 (单测/脚本) 可用。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# ── 内置缺省费率表 (与 unified_config.yaml broker.futu_us 保持一致) ──
_DEFAULT_SCHEDULE: Dict[str, Any] = {
    "commission_per_share": 0.01,
    "commission_min_per_order": 1.99,
    "commission_max_pct_of_amount": 0.003,
    "platform_fee": 0.0,
    "settlement_per_share": 0.003,
    # SEC §31: effective_from 时间序列 (升序), 按成交日取 <=trade_date 的最后一档
    "sec_fee_schedule": [
        {"from": "1900-01-01", "rate": 0.0},
        {"from": "2026-04-04", "rate": 0.0000206},
    ],
    "sec_fee_min": 0.01,
    "taf_per_share": 0.000195,
    "taf_min": 0.01,
    "taf_max": 9.79,
    "cat_per_share_nms": 0.000003,
    "cat_per_share_otc": 0.00000003,
    "borrow_day_basis": 365,  # 富途日息惯例; 360/365 差 1.4%, 量级不重要, 定标即可
}

_schedule_cache: Optional[Dict[str, Any]] = None


def get_schedule(refresh: bool = False) -> Dict[str, Any]:
    """费率表: config broker.futu_us 覆盖内置缺省 (浅合并, 缺键回退)。"""
    global _schedule_cache
    if _schedule_cache is not None and not refresh:
        return _schedule_cache
    sched = dict(_DEFAULT_SCHEDULE)
    try:
        from app.core.config_container import get_config
        override = get_config().get_dotted("broker.futu_us", None)
        if isinstance(override, dict):
            for k, v in override.items():
                if k in sched and v is not None:
                    sched[k] = v
    except Exception:
        pass  # 配置容器不可用 (脚本/单测环境) → 内置缺省
    _schedule_cache = sched
    return sched


def _sec_rate_for(trade_date: Optional[str], sched: Dict[str, Any]) -> float:
    """按成交日选 SEC 费率 (时间序列, 取 <=trade_date 的最后一档)。无日期按最新档。"""
    entries = sched.get("sec_fee_schedule") or []
    if not entries:
        return 0.0
    d = str(trade_date or "9999-12-31")[:10]
    rate = 0.0
    for e in sorted(entries, key=lambda x: str(x.get("from", ""))):
        if str(e.get("from", ""))[:10] <= d:
            rate = float(e.get("rate", 0) or 0)
    return rate


def estimate_fee(qty: float, price: float, side: str,
                 trade_date: Optional[str] = None, mkt: str = "NMS") -> Dict[str, float]:
    """单订单费用估算 → 分项明细 dict (含 total)。

    side: 'BUY'/'SELL' — 语义是订单的买卖方向, 非仓位方向:
      LONG 入场=BUY 出场=SELL; SHORT 入场=SELL(卖空开仓, SEC/TAF 照收) 出场=BUY。
    """
    qty = float(qty or 0)
    price = float(price or 0)
    if qty <= 0 or price <= 0:
        return {"commission": 0.0, "settlement": 0.0, "sec": 0.0, "taf": 0.0,
                "cat": 0.0, "total": 0.0}
    s = get_schedule()
    amt = qty * price
    commission = max(
        min(s["commission_per_share"] * qty, s["commission_max_pct_of_amount"] * amt),
        s["commission_min_per_order"],
    )
    settlement = s["settlement_per_share"] * qty
    cat_rate = s["cat_per_share_nms"] if str(mkt).upper() == "NMS" else s["cat_per_share_otc"]
    cat = cat_rate * qty
    sec = taf = 0.0
    if str(side).upper() == "SELL":
        sec_rate = _sec_rate_for(trade_date, s)
        if sec_rate > 0:
            sec = max(sec_rate * amt, s["sec_fee_min"])
        taf = min(max(s["taf_per_share"] * qty, s["taf_min"]), s["taf_max"])
    out = {"commission": round(commission, 4), "settlement": round(settlement, 4),
           "sec": round(sec, 4), "taf": round(taf, 4), "cat": round(cat, 6)}
    out["total"] = round(sum(out.values()), 4)
    return out


def estimate_round_trip(qty: float, entry_price: float, exit_price: float,
                        direction: str, trade_date: Optional[str] = None,
                        exit_date: Optional[str] = None,
                        mkt: str = "NMS") -> Dict[str, Any]:
    """一笔完整往返 (入场单+出场单) 费用。trade_records 每行=2 订单 (T2 实证 2/157 例外)。

    SHORT 方向对称: 入场腿=SELL (SEC/TAF 照收), 出场腿=BUY。
    """
    short = str(direction or "").upper() in ("SHORT", "SELL_SHORT", "SELL")
    entry_side, exit_side = ("SELL", "BUY") if short else ("BUY", "SELL")
    entry = estimate_fee(qty, entry_price, entry_side, trade_date, mkt)
    exit_ = estimate_fee(qty, exit_price, exit_side, exit_date or trade_date, mkt)
    return {"entry": entry, "exit": exit_,
            "total": round(entry["total"] + exit_["total"], 4)}


def accrue_borrow_fee(qty: float, price: float, annual_rate_pct: float,
                      hold_days: float, basis: Optional[int] = None) -> float:
    """SHORT 借券费计提: 市值 × 年率 × 持有天数/basis。日内 (hold_days<1 取整=0) 为 0。

    注: 历史回填拿不到历史费率, 只能用当日 get_margin_ratio 近似并打 borrow_rate_asof 标。
    """
    days = int(hold_days)  # 日内=0 天, 隔夜从 1 起算 (券商按自然日计息惯例)
    if days <= 0 or qty <= 0 or price <= 0 or annual_rate_pct <= 0:
        return 0.0
    b = basis or get_schedule()["borrow_day_basis"]
    return round(qty * price * (annual_rate_pct / 100.0) * days / b, 4)
