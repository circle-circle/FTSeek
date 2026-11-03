#!/opt/ftseek/FTSeek-US/venv/bin/python3
# -*- coding: utf-8 -*-
"""客单价 × 费率盈亏平衡账 (只读) — DEB-20260907 建议② 落地。

要回答的三个**算术确定**问题 (Claude 建议原话: "算小单费率盈亏平衡账")
----------------------------------------------------------------------
单笔名义额被三面夹住: 6% 单名帽 (`playbook.risk_parameters.max_position_pct`) 给上界、
`playbook.admission.min_notional_usd` 给下界、两个 0.5 乘子 (pipeline.py:4094 CAUTIOUS /
:4125 LONG 模型分歧, sim 采集期均已配置化为 1.0) 在中间砍。而费用不是比例税, 是**双地板**:

    fee_bps(N, P) ≈ max(39800/N, 200/P) + 62/P + 0.21

  · 39800/N — 每腿佣金地板 $1.99 × 2 腿 (qty ≤ 199 时恒付), 只随**名义额**下降
  · 200/P   — 每股佣金 $0.01 × 2 腿 (撞 0.3%×amt 上限前), 只随**股价**下降
  · 62/P    — 交收费 $0.003/股 ×2 腿 + TAF $0.000195/股, 同样只随股价下降
  · 0.21    — SEC §31 卖出腿 2.06e-5 × 金额

⇒ 两条地板**互不可替代**: 名义额再大也压不掉 262/P bps, 股价再高也压不掉 39800/N bps。
本脚本把这两条地板量化成可决策数字:
  Q1 在**当前可达名义带** [min_notional, equity×max_position_pct] 内, 费用地板是多少 bps?
  Q2 要把费用压到某个毛边际之下, 需要多大名义额 / 多高股价 / 多少净值? (三者可互相换算)
  Q3 已生成的指令里有多少落在地板线以下 (按股价段 / 名义段 / fee_bps 段分布)?

刻意不做的事 (归口, 防平行层)
----------------------------
· **不新建闸**。`app/playbook/admission.py` 已实现全部费用类判据并在
  `PlaybookDB.save_instruction` 收口 (2026-08-06 DEB-source-control)。当前生效档:
  `min_notional_usd=2500` / `min_entry_price=5.0` / `enforce=true`; 未启用档:
  `min_profit_fee_ratio=0.0` / `max_shares_per_order=0`。**档位反事实 ("若上调到 X 会拦掉
  哪些、净额如何变") 由既有 `scripts/admission_replay.py` 负责**, 本脚本不重复实现,
  只产出"该把档位定在哪个数字上"的算术依据。
· **不下"该拦"的结论**。Q1-Q3 全是算术量; 任何涉及已实现盈亏的对比一律标注 n 与极值
  敏感性 (宪章数据口径七律 #9: 跨信号统计必须报按日聚类值 + MDE)。
· **不声称费用是实收**。`trade_records.fees_source` 实测 399/399 = `est`, 即
  `pnl_net = realized_pnl − fee_model.estimate_round_trip()` ⇒ "模型费 vs 账本费" 的一致是
  **定义性同义反复**, 不构成对券商实收的验证 (SIM 期 order_fee_query 返空, 见
  fee_model.py 模块注释)。费率结构的正确性来自富途公示费率表, 不来自本对账。

用法:
  venv/bin/python scripts/ticket_size_breakeven.py                     # since 2026-07-11
  venv/bin/python scripts/ticket_size_breakeven.py --since 2026-08-25 --no-write
  venv/bin/python scripts/ticket_size_breakeven.py --equity 99200      # 覆写净值 (缺省读实测)
输出:
  data/shadow/ticket_size_breakeven_latest.json (fresh 覆写, 重跑幂等; --no-write 只打印)
零行为影响: 纯只读 (DB 以 mode=ro 打开), 不触碰交易路径, 不入 cron。
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

REPO = Path(__file__).resolve().parents[1]
# cron/手工均以 `venv/bin/python scripts/xxx.py` 调用 ⇒ sys.path[0]=scripts/,
# `from app.execution.fee_model import ...` 无法解析 → 仓根入 path (同款 bootstrap
# 见 scripts/trading_day_check.py / scripts/cf_pullback_limit_replay.py)。
sys.path.insert(0, str(REPO))

import yaml  # noqa: E402

from app.execution.fee_model import estimate_round_trip, get_schedule  # noqa: E402

DB_PATH = REPO / "data" / "ftseek_us.db"
UNIFIED_YAML = REPO / "config" / "unified_config.yaml"
OUT_LATEST = REPO / "data" / "shadow" / "ticket_size_breakeven_latest.json"

# 与 fee_unit_economics_scorecard.py / exit_scorecard.py 同口径 (宪章七律 #1)
PLAYBOOK_SOURCE_TYPES = ("PLAYBOOK_SIM", "PLAYBOOK_LIVE", "PLAYBOOK_SHADOW", "MANUAL_PLAYBOOK")
DEFAULT_SINCE = "2026-07-11"

# 代表性价位段 (美元)。$5 = 现行 admission.min_entry_price / UE min_price_floor,
# 即系统当前允许的最低价 — 放在首行正是为了看清地板有多高。
PRICE_GRID: Sequence[float] = (5.0, 10.0, 20.0, 30.0, 50.0, 100.0, 250.0, 600.0)
# 代表性名义额段 (美元)。5500 = **现行 admission.min_notional_usd** (2026-10-31 P3 用户裁
#   「P3上」: 2500→5500, DEB-20261031-budget-design §8) —— 必须含现行旋钮值, 否则
#   报告读不到"现在"在哪一格 (tests/test_ticket_size_breakeven.py 的网格覆盖守卫钉这条);
#   2500/5900 = 旧旋钮与 6%帽@~$99k 净值两端, 保留作历史对照。
NOTIONAL_GRID: Sequence[float] = (1000.0, 2500.0, 3000.0, 4200.0, 5000.0, 5500.0, 5900.0,
                                  8000.0, 12000.0, 25000.0, 50000.0)


# ────────────────────────────────────────────────────────────
# 纯函数层 (可单测, 零 IO)
# ────────────────────────────────────────────────────────────

def rt_fee_bps(notional: float, price: float, direction: str = "LONG",
               trade_date: Optional[str] = None) -> Optional[float]:
    """往返费用 bps — 直接调 fee_model SSOT (不自建费率公式, 防平行层)。"""
    if notional <= 0 or price <= 0:
        return None
    qty = notional / price
    fee = estimate_round_trip(qty, price, price, direction, trade_date)["total"]
    return 10000.0 * fee / notional


def fee_floor_decomposition(price: float) -> Dict[str, float]:
    """名义额→∞ 时的费用渐近线分解 (bps)。这是**股价单独决定的地板**, 加名义额压不掉。

    与 fee_model 同一张费率表 (get_schedule), 只把公式按项拆开以便解释; 数值与
    `rt_fee_bps(1000 × price, price)` 一致 (契约测试锁这条等价性)。

    ⚠ 口径边界: 本分解**不含 TAF 的 $9.79 上限**, 因为该上限只在 qty > 50,205 股
    (= 名义额 > 50,205 × 股价) 时才绑, 远高于本项目可达带 (≤ $6k); 若拿它去比一个
    上亿名义额的费用, TAF 项会被高估 ~0.2-0.4bps。同理佣金项假设未撞 0.3%×amt 上限
    (该上限只在股价 < $3.33 时绑)。
    """
    s = get_schedule()
    if price <= 0:
        return {}
    per_share_rt = 2.0 * float(s["commission_per_share"])          # 双腿每股佣金
    settlement_rt = 2.0 * float(s["settlement_per_share"])          # 双腿交收费
    taf = float(s["taf_per_share"])                                 # 仅卖出腿
    sec_rate = float((s.get("sec_fee_schedule") or [{}])[-1].get("rate", 0) or 0)
    return {
        "price": price,
        "commission_bps": 10000.0 * per_share_rt / price,
        "settlement_bps": 10000.0 * settlement_rt / price,
        "taf_bps": 10000.0 * taf / price,
        "sec_bps": 10000.0 * sec_rate,                              # 与价格无关 (金额比例)
        "asymptotic_fee_bps": 10000.0 * (per_share_rt + settlement_rt + taf) / price
                              + 10000.0 * sec_rate,
    }


def min_notional_for_fee_bps(target_bps: float, price: float,
                             hi: float = 1.0e9) -> Optional[float]:
    """fee_bps ≤ target 所需的最小名义额; 渐近线已高于 target 时返回 None (不可达)。

    fee_bps(N) 对 N 单调不增 (地板项 39800/N 递减, 其余与 N 无关) ⇒ 二分安全。
    """
    if target_bps <= 0 or price <= 0:
        return None
    if (rt_fee_bps(hi, price) or 0.0) > target_bps:
        return None
    lo = 1.0
    if (rt_fee_bps(lo, price) or 0.0) <= target_bps:
        return lo
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if (rt_fee_bps(mid, price) or 0.0) > target_bps:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1.0:
            break
    # 向上取整到分: 这是"最小要求", 舍入方向必须保守 (round 可能把值推回不达标一侧)
    return math.ceil(hi * 100.0) / 100.0


def min_price_for_fee_bps(target_bps: float, notional: float,
                          hi: float = 1.0e5) -> Optional[float]:
    """fee_bps ≤ target 所需的最低股价; 名义额地板已高于 target 时返回 None (不可达)。

    fee_bps(P) 对 P 单调不增 (每股项 262/P 递减, 佣金地板项与 P 无关) ⇒ 二分安全。
    """
    if target_bps <= 0 or notional <= 0:
        return None
    if (rt_fee_bps(notional, hi) or 0.0) > target_bps:
        return None
    lo = 0.01
    if (rt_fee_bps(notional, lo) or 0.0) <= target_bps:
        return lo
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if (rt_fee_bps(notional, mid) or 0.0) > target_bps:
            lo = mid
        else:
            hi = mid
        if hi - lo < 0.01:
            break
    return math.ceil(hi * 100.0) / 100.0


def equity_for_notional(target_notional: float, max_position_pct: float) -> Optional[float]:
    """要让 6% 单名帽够到 target_notional, 净值至少多大 (max_position_pct 可换算)。"""
    if target_notional <= 0 or max_position_pct <= 0:
        return None
    return round(target_notional / max_position_pct, 2)


def mean(xs: Sequence[float]) -> Optional[float]:
    xs = [float(x) for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def median(xs: Sequence[float]) -> Optional[float]:
    ys = sorted(float(x) for x in xs if x is not None)
    if not ys:
        return None
    k = len(ys) // 2
    return ys[k] if len(ys) % 2 else 0.5 * (ys[k - 1] + ys[k])


def excl_extreme_mean(xs: Sequence[float], k: int = 3) -> Optional[float]:
    """剔除 |值| 最大的 k 笔后的均值 — 军规 "任何分桶结论须在剔除前 3 笔后仍成立"。"""
    ys = sorted((float(x) for x in xs if x is not None), key=abs)
    ys = ys[: max(0, len(ys) - k)] if len(ys) > k else []
    return sum(ys) / len(ys) if ys else None


def bucketize(rows: List[Dict[str, Any]], key: str,
              edges: Sequence[float], value_key: str = "fee_bps") -> List[Dict[str, Any]]:
    """按 edges 分桶计数 (最后一个桶为上无界)。"""
    out: List[Dict[str, Any]] = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        sel = [r for r in rows if r.get(key) is not None and lo <= float(r[key]) < hi]
        vals = [r[value_key] for r in sel if r.get(value_key) is not None]
        out.append({"band": f"[{lo:g},{hi:g})", "n": len(sel),
                    "share_pct": round(100.0 * len(sel) / len(rows), 1) if rows else None,
                    f"mean_{value_key}": round(mean(vals), 2) if vals else None})
    return out


def notional_only_floor_bps(notional: float) -> Optional[float]:
    """与股价无关的名义额地板 = 每腿佣金地板 $1.99 x 2 腿 / 名义额 (bps)。

    这是**任何**标的在该名义额上都必须付的最低往返费 (高价股渐近线的主体项),
    也是 `admission.min_notional_usd` 这个旋钮的真实含义: 它不是在限风险,
    它是在定费用地板 — 2500 => 15.9bps, 3000 => 13.3bps, 5900 => 6.7bps。"""
    if notional <= 0:
        return None
    s = get_schedule()
    return round(10000.0 * 2.0 * float(s["commission_min_per_order"]) / notional, 2)


# ────────────────────────────────────────────────────────────
# 配置 / 数据装载 (只读)
# ────────────────────────────────────────────────────────────

def load_cfg() -> Dict[str, Any]:
    """读 unified_config.yaml 的三个既有旋钮组 (与 exit_replay_backtest._load_configs 同款直读)。"""
    uni = yaml.safe_load(UNIFIED_YAML.read_text()) or {}
    pb = uni.get("playbook", {}) or {}
    adm = pb.get("admission", {}) or {}
    risk = pb.get("risk_parameters", {}) or {}
    exe = pb.get("execution_parameters", {}) or {}
    return {
        "min_notional_usd": float(adm.get("min_notional_usd", 0) or 0),
        "min_entry_price": float(adm.get("min_entry_price", 0) or 0),
        "min_profit_fee_ratio": float(adm.get("min_profit_fee_ratio", 0) or 0),
        "max_shares_per_order": float(adm.get("max_shares_per_order", 0) or 0),
        "admission_enforce": bool(adm.get("enforce", False)),
        "max_position_pct": float(risk.get("max_position_pct", 0.06) or 0.06),
        "risk_base_capital": float(risk.get("risk_base_capital", 0) or 0),
        "cautious_mult": float(risk.get("cautious_mult", 1.0) or 1.0),
        "model_disagreement_downsize_factor": float(
            risk.get("model_disagreement_downsize_factor", 1.0) or 1.0),
        "confidence_sizing_enabled": bool(risk.get("confidence_sizing_enabled", False)),
        "daily_notional_cap_pct": float(exe.get("daily_notional_cap_pct", 0.8) or 0.8),
        "ue_min_price_floor": float((uni.get("universe_expansion", {}) or {}).get(
            "min_price_floor", 0) or 0),
    }


def _ro_conn() -> sqlite3.Connection:
    con = sqlite3.connect(Path(DB_PATH).as_uri() + "?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def current_equity(con: sqlite3.Connection) -> Optional[float]:
    """净值 — 读 `capital_state.current_equity`, 即 `_resolve_sizing_base` 所用 SSOT
    (`capital_safety.current_equity`, accinfo_query 每 30s 刷新) 的**持久化镜像**。
    离线脚本不取实时净值 (会打 Futu), 用镜像 + 落 `last_update` 供新鲜度判断。

    ⚠ 不可用 "历史最大成交名义 / cap" 反解净值: 全历史最大值混入了改帽前的旧口径
    (实测全历史 max $29,915 ⇒ 反解出 $498k 净值, 是窗内实测 max $5,944 的 5 倍),
    会把 "当前可达带" 放大 5 倍并让全部盈亏平衡结论失效。"""
    row = con.execute(
        """SELECT current_equity FROM capital_state
           WHERE current_equity > 0 ORDER BY last_update DESC LIMIT 1""").fetchone()
    return float(row[0]) if row and row[0] else None


def equity_asof(con: sqlite3.Connection) -> Optional[str]:
    """净值镜像的时间戳 — 陈旧镜像 (休市多日) 会让可达带偏, 必须随数字一起报。"""
    row = con.execute(
        """SELECT last_update FROM capital_state
           WHERE current_equity > 0 ORDER BY last_update DESC LIMIT 1""").fetchone()
    return str(row[0]) if row and row[0] else None


def window_max_notional(con: sqlite3.Connection, since: str) -> Optional[float]:
    """窗内最大成交名义额 — 净值镜像取不到时的**兜底**定标 (它本身就是 cap 的实测上界)。"""
    ph = ",".join("?" * len(PLAYBOOK_SOURCE_TYPES))
    row = con.execute(
        f"""SELECT MAX(entry_price * quantity) FROM trade_records
            WHERE source_type IN ({ph}) AND trade_date >= ?
              AND entry_price > 0 AND quantity > 0""",
        (*PLAYBOOK_SOURCE_TYPES, since)).fetchone()
    v = row[0] if row else None
    return float(v) if v else None


def load_instructions(con: sqlite3.Connection, since: str) -> List[Dict[str, Any]]:
    """已生成指令 (含未触发/未成交) 的名义额与股价 — Q3 的分母是**生成面**不是成交面,
    因为费率地板在生成时就已注定, 与是否成交无关。"""
    rows = con.execute(
        """SELECT date, symbol, direction, entry_price, position_size, take_profit,
                  triggered, executed, skip_reason
           FROM playbook_instructions
           WHERE date >= ? AND entry_price > 0 AND position_size > 0""",
        (since,)).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        notional = float(r["entry_price"]) * float(r["position_size"])
        d = str(r["direction"] or "").upper()
        dirn = "SHORT" if d in ("SHORT", "SELL_SHORT", "SELL") else "LONG"
        fee_bps = rt_fee_bps(notional, float(r["entry_price"]), dirn, r["date"])
        out.append({
            "date": r["date"], "symbol": r["symbol"], "direction": d,
            "price": float(r["entry_price"]), "shares": float(r["position_size"]),
            "notional": round(notional, 2), "fee_bps": round(fee_bps, 2) if fee_bps else None,
            "triggered": bool(r["triggered"]), "executed": bool(r["executed"]),
            "skip_reason": r["skip_reason"],
        })
    return out


def load_trades(con: sqlite3.Connection, since: str) -> List[Dict[str, Any]]:
    """已平仓成交 — 只用于给 "实测毛边际" 定标 (Q2 的 target_bps 输入), 不做反事实。"""
    ph = ",".join("?" * len(PLAYBOOK_SOURCE_TYPES))
    rows = con.execute(
        f"""SELECT trade_date, symbol, direction, entry_price, quantity,
                  realized_pnl, pnl_net, fees_est
            FROM trade_records
            WHERE source_type IN ({ph}) AND trade_date >= ?
              AND exit_time IS NOT NULL AND exit_time != ''
              AND entry_price > 0 AND quantity > 0""",
        (*PLAYBOOK_SOURCE_TYPES, since)).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        notional = float(r["entry_price"]) * float(r["quantity"])
        gross = float(r["realized_pnl"] or 0.0)
        net = float(r["pnl_net"] if r["pnl_net"] is not None else gross)
        out.append({
            "date": r["trade_date"], "symbol": r["symbol"], "notional": round(notional, 2),
            "price": float(r["entry_price"]),
            "gross_bps": round(10000.0 * gross / notional, 2),
            "fee_bps_ledger": round(10000.0 * float(r["fees_est"] or (gross - net)) / notional, 2),
            "fee_bps_model": round(rt_fee_bps(notional, float(r["entry_price"]),
                                              str(r["direction"] or "LONG"), r["trade_date"]) or 0.0, 2),
            "gross": gross, "net": net,
        })
    return out


# ────────────────────────────────────────────────────────────
# 报告

def build_report(since: str, equity_override: Optional[float] = None) -> Dict[str, Any]:
    cfg = load_cfg()
    con = _ro_conn()
    try:
        equity_mirror = current_equity(con)
        equity_mirror_asof = equity_asof(con)
        win_max_notional = window_max_notional(con, since)
        instructions = load_instructions(con, since)
        trades = load_trades(con, since)
    finally:
        con.close()

    # 当前可达名义带: 上界 = 净值 × 单名帽。净值口径优先级 (与 _resolve_sizing_base 同序):
    #   ① CLI 覆写 ② capital_state.current_equity 持久化镜像 ③ 窗内最大成交名义 (= 帽的实测上界)
    #   ④ config risk_base_capital。②③④ 都把来源写进报告, 禁止混用 (宪章七律 #4)。
    cap_pct = cfg["max_position_pct"]
    if equity_override:
        equity = float(equity_override)
        equity_src = "cli_override"
    elif equity_mirror:
        equity = equity_mirror
        equity_src = f"capital_state.current_equity@{equity_mirror_asof}"
    elif win_max_notional and cap_pct > 0:
        equity = win_max_notional / cap_pct
        equity_src = f"window_max_notional/{cap_pct:g}(fallback)"
    else:
        equity = cfg["risk_base_capital"]
        equity_src = "config.risk_base_capital(fallback)"
    notional_hi = round(equity * cap_pct, 2)
    notional_lo = cfg["min_notional_usd"]

    gross_bps = [t["gross_bps"] for t in trades]
    tot_gross = sum(t["gross"] for t in trades)
    tot_notional = sum(t["notional"] for t in trades)
    measured = {
        "n_trades": len(trades),
        # 名义加权 = fee_unit_economics_scorecard 口径 (gross_total/total_notional), 可直接对齐既有记分牌
        "gross_bps_notional_weighted": round(10000.0 * tot_gross / tot_notional, 2) if tot_notional else None,
        # 等权 = 每笔一票, 与中位数一起暴露偏度 (本项目分桶结论必须两种口径并报)
        "gross_bps_mean": round(mean(gross_bps), 2) if gross_bps else None,
        "gross_bps_median": round(median(gross_bps), 2) if gross_bps else None,
        "gross_bps_mean_excl_top3": round(excl_extreme_mean(gross_bps, 3), 2) if gross_bps else None,
        "gross_bps_notional_weighted_excl_top3": round(
            10000.0 * sum(t["gross"] for t in sorted(trades, key=lambda x: abs(x["gross"]))[:-3])
            / max(1.0, sum(t["notional"] for t in sorted(trades, key=lambda x: abs(x["gross"]))[:-3])), 2)
            if len(trades) > 3 else None,
        "gross_total": round(tot_gross, 2),
        "notional_total": round(tot_notional, 2),
        "net_total": round(sum(t["net"] for t in trades), 2),
        "not_lt_30_flag": len(trades) < 30,
    }

    # Q1: 可达带内的费用地板 (带内最优点 = 名义上界 × 股价→∞ 的渐近线)
    q1 = {
        "notional_band_usd": [notional_lo, notional_hi],
        "equity_usd": round(equity, 2),
        "equity_source": equity_src,
        "fee_bps_at_band_hi_by_price": {
            str(p): round(rt_fee_bps(notional_hi, p) or 0.0, 2) for p in PRICE_GRID},
        "fee_bps_at_band_lo_by_price": {
            str(p): round(rt_fee_bps(notional_lo, p) or 0.0, 2) for p in PRICE_GRID},
        "band_floor_bps": round(min(
            (rt_fee_bps(notional_hi, p) or 1e9) for p in PRICE_GRID), 2),
        "commission_floor_only_bps_at_band_hi": notional_only_floor_bps(notional_hi),
        # 名义额地板阶梯 — 与股价无关的那一半: min_notional_usd 这个旋钮的真实含义
        "notional_floor_ladder": [
            {"notional_usd": n, "commission_floor_bps": notional_only_floor_bps(n),
             "fee_bps_at_price_600": round(rt_fee_bps(n, 600.0) or 0.0, 2)}
            for n in (1000.0, 1500.0, 2000.0, 2500.0, 3000.0, 4000.0, 5000.0,
                      5900.0, 8000.0, 12000.0, 25000.0)],
        "band_lo_is_binding_floor": bool(
            (notional_only_floor_bps(notional_lo) or 0.0)
            > (notional_only_floor_bps(notional_hi) or 0.0)),
    }

    # Q2: target 档 = "毛边际必须盖过的费用" 的显式网格 + 实测毛边际两口径。
    #   实测值随窗口变化, 故打上 label 与来源, 不与固定档混排 (宪章七律 #4)。
    targets: List[Dict[str, Any]] = []
    for lab, v in (("measured_notional_weighted", measured["gross_bps_notional_weighted"]),
                   ("measured_per_trade_median", measured["gross_bps_median"]),
                   ("fixed_3bps", 3.0), ("fixed_5bps", 5.0), ("fixed_10bps", 10.0),
                   ("fixed_15bps", 15.0), ("fixed_20bps", 20.0)):
        if v is None:
            continue
        if any(abs(t["target_gross_bps"] - round(float(v), 2)) < 0.005 for t in targets):
            continue
        targets.append({"label": lab, "target_gross_bps": round(float(v), 2)})
    q2 = []
    for t in targets:
        tgt = t["target_gross_bps"]
        n_star = min_notional_for_fee_bps(tgt, 600.0)   # 高价股 ⇒ 只剩名义额地板
        row = {
            "label": t["label"],
            "target_gross_bps": tgt,
            "min_notional_usd_price600": n_star,
            "min_equity_usd_at_cap": equity_for_notional(n_star, cap_pct) if n_star else None,
            "reachable_within_band": bool(n_star is not None and n_star <= notional_hi),
            "min_price_at_band_hi": min_price_for_fee_bps(tgt, notional_hi),
            "min_price_at_band_lo": min_price_for_fee_bps(tgt, notional_lo),
        }
        q2.append(row)

    # Q3: 生成面分布 (指令级, 非成交级)
    floor_bps = q1["band_floor_bps"]
    # 阈值 = 实测毛边际 (名义加权口径, 与既有记分牌可对齐)。"fee_bps > 毛边际" 的单
    # 在**生成时**就注定毛利盖不过费用, 与选股/择时/执行无关 ⇒ 这是算术量不是统计推断。
    thr = measured["gross_bps_notional_weighted"]
    thr_ladder = [x for x in (thr, 5.0, 8.0, 10.0, 15.0) if x is not None]
    q3 = {
        "n_instructions": len(instructions),
        "notional_mean": round(mean([i["notional"] for i in instructions]), 2) if instructions else None,
        "notional_median": round(median([i["notional"] for i in instructions]), 2) if instructions else None,
        "by_notional": bucketize(instructions, "notional",
                                 (0, 1500, 2500, 3000, 4200, 5000, 5900, 1e9)),
        "by_price": bucketize(instructions, "price", (0, 5, 10, 20, 30, 60, 150, 1e9)),
        "by_fee_bps": bucketize(instructions, "fee_bps", (0, 5, 8, 10, 15, 25, 1e9)),
        "threshold_bps": thr,
        "n_fee_above_gross_by_threshold": [
            {"threshold_bps": round(float(x), 2),
             "n": sum(1 for i in instructions
                      if i["fee_bps"] is not None and i["fee_bps"] > float(x)),
             "share_pct": round(100.0 * sum(1 for i in instructions
                                            if i["fee_bps"] is not None and i["fee_bps"] > float(x))
                                / len(instructions), 1) if instructions else None}
            for x in dict.fromkeys(thr_ladder)],
        "n_above_band_floor": sum(1 for i in instructions
                                  if i["fee_bps"] is not None and i["fee_bps"] >= floor_bps),
        "note": "生成面 (含未触发/未成交): 费率地板在指令生成时即已注定, 与是否成交无关。",
    }

    # 费率模型 vs 账本费用 (定义性同义反复, 显式披露而非当作验证)
    model_vs_ledger = {
        "n": len(trades),
        "mean_abs_diff_bps": round(mean([abs(t["fee_bps_model"] - t["fee_bps_ledger"])
                                         for t in trades]), 4) if trades else None,
        "caveat": "fees_source 全为 est ⇒ pnl_net 就是本模型算出来的, 一致是同义反复, "
                  "不构成对券商实收的验证。",
    }

    return {
        "script": "ticket_size_breakeven",
        "since": since,
        "read_only": True,
        "cfg_snapshot": cfg,
        "fee_schedule": {k: v for k, v in get_schedule().items()},
        "asymptotic_floor_by_price": [
            {**fee_floor_decomposition(p),
             "asymptotic_fee_bps": round(fee_floor_decomposition(p)["asymptotic_fee_bps"], 2)}
            for p in PRICE_GRID],
        "measured_gross": measured,
        "q1_reachable_band_floor": q1,
        "q2_breakeven_conversion": q2,
        "q3_generated_distribution": q3,
        "fee_model_vs_ledger": model_vs_ledger,
        "verdict": _verdict(cfg, measured, q1, q2, q3, equity, cap_pct),
        "handoff": {
            "档位反事实归口": "scripts/admission_replay.py (既有, 勿重复实现)",
            "既有可调旋钮": ["playbook.admission.min_notional_usd",
                             "playbook.admission.min_entry_price",
                             "playbook.admission.min_profit_fee_ratio",
                             "playbook.admission.max_shares_per_order",
                             "playbook.risk_parameters.max_position_pct"],
            "本脚本不做": "不改配置、不改代码、不下 '该拦' 的结论 — 只给档位算数字。",
        },
    }


def _verdict(cfg: Dict[str, Any], m: Dict[str, Any], q1: Dict[str, Any],
             q2: List[Dict[str, Any]], q3: Dict[str, Any],
             equity: float, cap_pct: float) -> Dict[str, Any]:
    """把结论按**证据类型**分层: 算术确定量 vs 窗口统计量, 绝不混叙 (宪章七律 #9)。"""
    band_lo, band_hi = q1["notional_band_usd"]
    tgt_row = next((r for r in q2 if r["label"] == "measured_notional_weighted"), None)
    n_need = tgt_row["min_notional_usd_price600"] if tgt_row else None
    eq_need = tgt_row["min_equity_usd_at_cap"] if tgt_row else None
    cap_need = round(100.0 * n_need / equity, 2) if (n_need and equity > 0) else None
    return {
        # ── A. 算术确定 (与窗口/样本/模型质量无关, 只依赖富途公示费率表) ──
        "mechanical_certain": [
            "费用是双地板: fee_bps ≈ max(39800/N, 262/P) + 0.21。两条地板互不可替代 — "
            "加名义额压不掉股价项, 换高价股压不掉名义额项。",
            f"现行可达名义带 ${band_lo:,.0f}~${band_hi:,.0f} 内, 往返费地板 = "
            f"{q1['band_floor_bps']}bps (带上界+最优股价) ~ "
            f"{notional_only_floor_bps(band_lo)}bps (带下界, 任何股价都躲不掉)。",
            f"admission.min_notional_usd={cfg['min_notional_usd']:g} 的真实语义是费用地板 "
            f"{notional_only_floor_bps(cfg['min_notional_usd'])}bps; RFC-071 把它从 3000 降到 2500 "
            f"= 把费用地板从 {notional_only_floor_bps(3000.0)}bps 抬到 "
            f"{notional_only_floor_bps(2500.0)}bps (换吞吐量, 代价是单位经济学)。",
            f"股价 < $20 的标的在任何名义额下都压不到 13.3bps 以下 (渐近线 262/P); "
            f"现行 min_entry_price=${cfg['min_entry_price']:g} 恰好落在渐近线 "
            f"{round(fee_floor_decomposition(cfg['min_entry_price'])['asymptotic_fee_bps'], 1) if cfg['min_entry_price'] > 0 else None}bps "
            f"= 全表最贵的一档。",
        ],
        # ── B. 窗口统计量 (随窗口/极值变化, 必须与 A 分开读) ──
        "measured_statistical": {
            "gross_bps_notional_weighted": m["gross_bps_notional_weighted"],
            "gross_bps_notional_weighted_excl_top3": m["gross_bps_notional_weighted_excl_top3"],
            "gross_bps_per_trade_median": m["gross_bps_median"],
            "n_trades": m["n_trades"],
            "caveat": "毛边际是窗口统计量, 剔极值后从 "
                      f"{m['gross_bps_notional_weighted']}→{m['gross_bps_notional_weighted_excl_top3']}bps "
                      "(翻过带内地板) ⇒ A/B 对比在带上界处是**边际**的, 不可当定论; "
                      "带下界处 (15.9bps) 则远高于任何毛边际口径, 是确定的。",
        },
        # ── C. 由 A 直接推出的可达性换算 (不含任何统计假设) ──
        "escape_hatches": {
            "所需净值_让带上界够到实测毛边际": eq_need,
            "当前净值": round(equity, 2),
            "净值缺口_pct": round(100.0 * (eq_need / equity - 1.0), 1) if (eq_need and equity) else None,
            "或_所需单名帽_pct": cap_need,
            "当前单名帽_pct": round(100.0 * cap_pct, 2),
            "或_提高每笔毛边际到_bps": round(q1["band_floor_bps"], 2),
            "说明": "三者等价可换 — 加净值/加帽是同一件事 (名义上界=净值×帽), "
                    "第三条则是改持有期或改选股 (更长持有期 ⇒ 更大毛波动 ⇒ 同一笔费用占比更低)。",
        },
        "generated_share_above_10bps_pct": next(
            (r["share_pct"] for r in q3["n_fee_above_gross_by_threshold"]
             if abs(r["threshold_bps"] - 10.0) < 0.005), None),
    }


def print_report(rep: Dict[str, Any]) -> None:
    m = rep["measured_gross"]
    q1 = rep["q1_reachable_band_floor"]
    print("=" * 78)
    print(f"客单价 × 费率盈亏平衡账 (since {rep['since']}, 只读)")
    print("=" * 78)
    print(f"\n[实测毛边际] n={m['n_trades']}  名义加权 {m['gross_bps_notional_weighted']}bps  "
          f"等权均值 {m['gross_bps_mean']}bps  等权中位 {m['gross_bps_median']}bps")
    print(f"  剔|毛|最大3笔: 名义加权 {m['gross_bps_notional_weighted_excl_top3']}bps / "
          f"等权 {m['gross_bps_mean_excl_top3']}bps  "
          f"(毛 ${m['gross_total']:+,.2f} / 名义 ${m['notional_total']:,.0f} / 净 ${m['net_total']:+,.2f})")
    if m["not_lt_30_flag"]:
        print("  ⚠ n<30 — 宪章七律 #5: 不下结论")
    lo, hi = q1["notional_band_usd"]
    print(f"\n[Q1 当前可达名义带] ${lo:,.0f} ~ ${hi:,.0f}  "
          f"(净值 ${q1['equity_usd']:,.0f} × 帽 {rep['cfg_snapshot']['max_position_pct']:.0%}, "
          f"来源={q1['equity_source']})")
    print(f"  仅佣金地板 ($1.99×2腿) 在名义上界处 = {q1['commission_floor_only_bps_at_band_hi']}bps "
          f"/ 在名义下界处 = {notional_only_floor_bps(lo)}bps")
    print(f"  带内费用地板 (最优股价) = {q1['band_floor_bps']}bps")
    print("  [名义额地板阶梯] (与股价无关的那一半 — min_notional_usd 的真实含义)")
    print("  %-12s %16s %16s" % ("名义额$", "佣金地板bps", "P=$600 实际bps"))
    for r in q1["notional_floor_ladder"]:
        mark = ""
        if abs(r["notional_usd"] - lo) < 1:
            mark = "  ← 现行 admission.min_notional_usd"
        elif abs(r["notional_usd"] - hi) < 60:
            mark = "  ← 现行单名帽上界"
        print("  %-12s %16s %16s%s" % (f"{r['notional_usd']:,.0f}",
                                       r["commission_floor_bps"], r["fee_bps_at_price_600"], mark))
    print("  %-8s %10s %10s" % ("股价$", "带下界bps", "带上界bps"))
    for p in PRICE_GRID:
        print("  %-8s %10s %10s" % (
            f"{p:g}", q1["fee_bps_at_band_lo_by_price"][str(p)],
            q1["fee_bps_at_band_hi_by_price"][str(p)]))
    print("\n[渐近地板] 名义额→∞ 也压不掉的股价项 (262/P bps):")
    for row in rep["asymptotic_floor_by_price"]:
        print("  P=$%-6g 佣金 %6.2f + 交收 %5.2f + TAF %4.2f + SEC %4.2f = %6.2f bps" % (
            row["price"], row["commission_bps"], row["settlement_bps"],
            row["taf_bps"], row["sec_bps"], row["asymptotic_fee_bps"]))
    print("\n[Q2 盈亏平衡换算] target = 毛边际必须盖过的费用档")
    print("  %-26s %9s %14s %13s %8s %12s %12s" % (
        "target", "bps", "所需名义$(P=600)", "所需净值$@帽", "带内可达", "最低价$@带上界", "最低价$@带下界"))
    for r in rep["q2_breakeven_conversion"]:
        print("  %-26s %9s %14s %13s %8s %12s %12s" % (
            r["label"], f"{r['target_gross_bps']:g}",
            f"{r['min_notional_usd_price600']:,.0f}" if r["min_notional_usd_price600"] else "不可达",
            f"{r['min_equity_usd_at_cap']:,.0f}" if r["min_equity_usd_at_cap"] else "—",
            "是" if r["reachable_within_band"] else "否",
            f"{r['min_price_at_band_hi']:g}" if r["min_price_at_band_hi"] else "不可达",
            f"{r['min_price_at_band_lo']:g}" if r["min_price_at_band_lo"] else "不可达"))
    q3 = rep["q3_generated_distribution"]
    print(f"\n[Q3 生成面分布] 指令 n={q3['n_instructions']}  名义均值 ${q3['notional_mean']:,.0f} "
          f"中位 ${q3['notional_median']:,.0f}")
    for lab, key in (("名义额$", "by_notional"), ("股价$", "by_price"), ("fee_bps", "by_fee_bps")):
        parts = [f"{b['band']}: n={b['n']}({b['share_pct']}%)" for b in q3[key] if b["n"]]
        print(f"  {lab:9s} " + " | ".join(parts))
    print("  fee_bps 高于各毛边际档的指令占比 (在生成时即注定毛利盖不过费用, 与选股择时无关):")
    for r in q3["n_fee_above_gross_by_threshold"]:
        tag = "  ← 实测毛边际" if abs(r["threshold_bps"] - (q3["threshold_bps"] or 0)) < 0.005 else ""
        print(f"    fee_bps > {r['threshold_bps']:>6g}bps : {r['n']:>4}/{q3['n_instructions']} "
              f"({r['share_pct']}%){tag}")
    ml = rep["fee_model_vs_ledger"]
    print(f"\n[模型 vs 账本费用] n={ml['n']} 平均绝对差 {ml['mean_abs_diff_bps']}bps — {ml['caveat']}")
    v = rep["verdict"]
    print("\n" + "=" * 78)
    print("[结论 A] 算术确定 (只依赖费率表, 与窗口/样本/模型质量无关)")
    for s in v["mechanical_certain"]:
        print(f"  · {s}")
    b = v["measured_statistical"]
    print(f"\n[结论 B] 窗口统计量 (n={b['n_trades']}, 必须与 A 分开读)")
    print(f"  毛边际: 名义加权 {b['gross_bps_notional_weighted']}bps / 剔极值 "
          f"{b['gross_bps_notional_weighted_excl_top3']}bps / 等权中位 {b['gross_bps_per_trade_median']}bps")
    print(f"  ⚠ {b['caveat']}")
    e = v["escape_hatches"]
    print("\n[结论 C] 可达性换算 (由 A 直接推出, 无统计假设)")
    print(f"  要让带上界够到实测毛边际, 三选一: 净值 ${e['当前净值']:,.0f} → "
          f"${e['所需净值_让带上界够到实测毛边际']:,.0f} "
          f"(+{e['净值缺口_pct']}%) / 单名帽 {e['当前单名帽_pct']}% → {e['或_所需单名帽_pct']}% / "
          f"每笔毛边际 → {e['或_提高每笔毛边际到_bps']}bps")
    print(f"  当前生成指令中 fee_bps>10bps 的占比: {v['generated_share_above_10bps_pct']}%")
    print("\n[归口] 档位反事实 (若上调到 X 拦掉哪些/净额如何变) → scripts/admission_replay.py")
    print("       本脚本只给算术地板, 不改配置、不下 '该拦' 结论。")


def main() -> int:
    ap = argparse.ArgumentParser(description="客单价 × 费率盈亏平衡账 (只读)")
    ap.add_argument("--since", default=DEFAULT_SINCE, help="窗口起始 trade_date/instruction date")
    ap.add_argument("--equity", type=float, default=None, help="覆写净值 (缺省由实测最大成交名义反解)")
    ap.add_argument("--no-write", action="store_true", help="只打印, 不写 data/shadow/*.json")
    ap.add_argument("--json", default=str(OUT_LATEST), help="输出路径")
    args = ap.parse_args()

    rep = build_report(args.since, args.equity)
    print_report(rep)
    if not args.no_write:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
        print(f"\n[write] {out} (fresh 覆写, 重跑幂等)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
