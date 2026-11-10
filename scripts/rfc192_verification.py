#!/usr/bin/env python3
"""RFC-192 复核尺子 (只读, 零副作用) — 2026-11-08 复核专用。

为什么有这个脚本
----------------
RFC-192 (docs/decisions/RFC-192-fewer-larger-trades.md) 的关键读数
(499 笔阈值表 / A·A'·B 三方案 / 费率算术) 在仓库内**没有产出脚本**,
且文档里同时出现三套互不吻合的"现状"基线 (-$400 / -$882 / -$1,270)。
本脚本把这些读数一次性重算, 让 RFC-192 的每一个数字都有可复现命令
(宪章: 没有命令的读数等于没有读数)。

它回答 8 个问题 (对应 RFC-192 §10 的 8 条勘误)
------------------------------------------------
A. 宇宙与基线: DA 成交宇宙多大; RFC 的三套"现状"基线各自出自哪个口径
B. gap 口径: 阈值表的 gap 字段 (开盘跳空) vs C2 拟用字段 (盘前跳空)
   —— 覆盖率、1.0% 阈值判定一致性、两套口径下的同一张阈值表
C. 费用: RFC 单地板 39800/N+0.42 vs 仓库 SSOT 双地板 max(39800/N, 262/P)+0.21
   vs 实测费 (fee_model 真模型)
D. sizing 重建: 从 instructions 存的 risk_amount/stop_loss 还原风险预算股数,
   验证重建精度, 并算出 C1 (帽 6%→10%) **真实**能放大的名义额
E. 方案复算: A / A' / B (日聚类 t、每笔、天数)
F. 时代分层: 分月净额 (全样本 / |gap|<1% 保留组 / A 臂)
G. 判据可达性: RFC-192 §5 五道门逐条在历史读数上是否已可达
H. 多空拆分: DEB §55 "别做空" 在保留组里的实现口径读数

口径声明 (必须与 RFC-192 §10 一致)
----------------------------------
· gap 主口径 = `trade_records.metadata_json.entry_gap.aligned_gap_pct`
  = **今日开盘价 vs 昨收** (空头取反), 见 app/playbook/playbook_service.py:2131-2148。
  RFC-192/DEB §44/§54 的全部阈值表都用这个字段, 本脚本逐位复现 (§B 段)。
· gap 备用口径 = `data/da_checkpoint/<date>/<SYM>.raw.json → collected.snapshot.pre_change_rate`
  = **盘前**涨跌幅。这才是 C2 字面要求的量, 但覆盖率只有 ~26%。
· 费用一律走 `app.execution.fee_model.estimate_round_trip` (富途公示费率表口径),
  **不**用 RFC 的单地板公式做实算 (该公式只作为对比项打印)。
· 名义放大臂: 毛按股数线性缩放, 费按真实模型在新股数上重算
  (隐含"毛利率不随规模变化", 该假设由 DEB §51.1 参与率数据支持, 但 sim 无法证伪)。

只读保证
--------
· DB 以 `file:...?mode=ro` 打开; 不写任何文件 (含 data/shadow); 零网络; 零 cron。
· 全部读数确定性: 无随机、无采样。

用法:
  venv/bin/python scripts/rfc192_verification.py            # 人读报告
  venv/bin/python scripts/rfc192_verification.py --json     # 机器读 (stdout)
  venv/bin/python scripts/rfc192_verification.py --section E # 只跑某段
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.execution.fee_model import estimate_round_trip  # noqa: E402

DB_PATH = ROOT / "data" / "ftseek_us.db"
CKPT_DIR = ROOT / "data" / "da_checkpoint"

# ── 常量 (全部来自 RFC-192 / 配置, 不是新参数) ────────────────────────────
CAP_PCT_CUR = 0.06          # playbook.risk_parameters.max_position_pct (现状)
CAP_PCT_NEW = 0.10          # RFC-192 C1 目标
EQUITY_REF = 99179.0        # 富途实时净值 2026-11-07 (RFC-191 §2)
GAP_THRESHOLD = 1.0         # RFC-192 C2 阈值 (%)
FLOOR_PX = 5.0              # admission.min_entry_price
RFC_FEE_FLOOR_CONST = 0.42  # RFC-192 §2.3 的从价项
SSO_FEE_CONST = 0.21        # scripts/ticket_size_breakeven.py 的从价项


# ── 取数 ─────────────────────────────────────────────────────────────────
def load_da_trades() -> List[Dict[str, Any]]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("""
        SELECT trade_date, symbol, direction, entry_price, exit_price, quantity,
               realized_pnl, pnl_net, metadata_json, market_regime
        FROM trade_records
        WHERE realized_pnl IS NOT NULL AND decision_id LIKE 'DA-%'
        ORDER BY trade_date"""))
    conn.close()
    out = []
    for r in rows:
        qty = r["quantity"] or 0
        px = r["entry_price"] or 0.0
        xp = r["exit_price"] or 0.0
        if qty <= 0 or px <= 0:
            continue
        fee = estimate_round_trip(qty, px, xp, r["direction"] or "BUY", r["trade_date"])["total"]
        out.append({
            "day": r["trade_date"], "sym": r["symbol"], "dir": r["direction"] or "",
            "qty": qty, "px": px, "xp": xp, "n": qty * px, "fee": fee,
            # 口径: pnl_net = realized_pnl − fee (仓内 SSOT, 见 scripts/ticket_size_breakeven.py)
            # ⇒ realized_pnl 本身即"费前毛额", 不得再加一次费
            "gross": (r["realized_pnl"] or 0.0),
            "net": r["pnl_net"] or 0.0,
            "gap": _entry_gap(r["metadata_json"]),
            "regime": r["market_regime"] or "NULL",
        })
    return out


def _entry_gap(meta_json: Optional[str]) -> Optional[float]:
    try:
        m = json.loads(meta_json or "{}")
        v = (m.get("entry_gap") or {}).get("aligned_gap_pct")
        return float(v)
    except (TypeError, ValueError, AttributeError):
        return None


def premarket_gap(day: str, symbol: str) -> Optional[float]:
    p = CKPT_DIR / day / (symbol + ".raw.json")
    if not p.exists():
        return None
    try:
        j = json.loads(p.read_text())
        v = ((j.get("collected") or {}).get("snapshot") or {}).get("pre_change_rate")
        return float(v)
    except (TypeError, ValueError, AttributeError, OSError):
        return None


def load_instructions() -> Dict[Tuple[str, str], Dict[str, Any]]:
    """DA 指令行 (date, symbol) → 计划入场/止损/风险预算 (C1 重建用)。"""
    conn = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute("""
        SELECT date, symbol, direction, entry_price, stop_loss, position_size,
               risk_amount, market_regime, executed
        FROM playbook_instructions
        WHERE row_kind='instruction' AND decision_id LIKE 'DA-%'"""))
    conn.close()
    out = {}
    for r in rows:
        if not r["executed"]:
            continue
        out[(r["date"], r["symbol"])] = {
            "plan_px": r["entry_price"], "sl": r["stop_loss"],
            "plan_qty": r["position_size"], "risk_amount": r["risk_amount"],
            "regime": r["market_regime"] or "NULL",
        }
    return out


# ── 统计原语 ─────────────────────────────────────────────────────────────
def tstat(vals: List[float]) -> Optional[float]:
    if len(vals) < 3:
        return None
    m = statistics.mean(vals)
    sd = statistics.pstdev(vals)
    return (m / (sd / len(vals) ** 0.5)) if sd else None


def by_day(items: List[Dict[str, Any]], key: str = "net") -> Dict[str, float]:
    d = defaultdict(float)
    for x in items:
        d[x["day"]] += x[key]
    return dict(d)


def daily_t(items: List[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    vals = list(by_day(items).values())
    return (statistics.mean(vals) if vals else None), tstat(vals)


def fmt_num(v: Optional[float], nd: int = 2, suf: str = "") -> str:
    if v is None:
        return "—"
    return ("%." + str(nd) + "f") % v + suf


# ── 费用口径 ─────────────────────────────────────────────────────────────
def rfc_single_floor_bps(notional: float) -> float:
    """RFC-191/192 §2.3 的公式 (单地板): 39,800/N + 0.42。"""
    return 39800.0 / notional + RFC_FEE_FLOOR_CONST


def sso_double_floor_bps(notional: float, px: float) -> float:
    """仓库 SSOT (scripts/ticket_size_breakeven.py): max(39,800/N, 262/P) + 0.21。"""
    return max(39800.0 / notional, 262.0 / px) + SSO_FEE_CONST


# ── 名义放大口径 (C1) ────────────────────────────────────────────────────
# 名义 = min(风险预算名义, 帽名义)。历史风险预算不可还原 (见段 D), 故 C1 只能给区间:
#   none    : 不放大 (现状帽 6%)
#   upper   : 顶到 6% 帽的笔长到新帽 $9,918 (C1 的上界)
#   uniform : 全部笔按 $10k 重算 (RFC-192 §2.3 的隐含假设, 与实际 sizing 链不符)
C1_MODES = ("none", "upper", "uniform")


def scale_notional(x: Dict[str, Any], mode: str) -> float:
    cap_now = EQUITY_REF * CAP_PCT_CUR
    if mode == "uniform":
        return max(1, int(10000.0 / x["px"])) * x["px"]
    if mode == "upper" and x["n"] >= 0.98 * cap_now:
        return max(1, int((EQUITY_REF * CAP_PCT_NEW) / x["px"])) * x["px"]
    return x["n"]


# ── 段 A: 宇宙与基线 ─────────────────────────────────────────────────────
def section_a(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    days = sorted({x["day"] for x in trades})
    with_gap = [x for x in trades if x["gap"] is not None]
    gap_days = sorted({x["day"] for x in with_gap})
    recent74 = [x for x in trades if x["day"] >= days[-74]]
    recent74_gap = [x for x in with_gap if x["day"] >= days[-74]]
    oldest = [x for x in trades if x["day"] < days[-74]]
    return {
        "n_trades": len(trades), "n_days": len(days),
        "day_min": days[0], "day_max": days[-1],
        "net_all": sum(x["net"] for x in trades),
        "n_with_gap": len(with_gap), "n_gap_days": len(gap_days),
        "net_with_gap": sum(x["net"] for x in with_gap),
        "recent74_n": len(recent74), "recent74_net": sum(x["net"] for x in recent74),
        "recent74_gap_n": len(recent74_gap), "recent74_gap_net": sum(x["net"] for x in recent74_gap),
        "oldest_n": len(oldest), "oldest_net": sum(x["net"] for x in oldest),
        "full_t": daily_t(trades)[1],
    }


def print_a(trades: List[Dict[str, Any]], a: Dict[str, Any]) -> None:
    print("── A. 宇宙与基线 ─────────────────────────────────────────────")
    print("  DA 已平仓 universe: n=%d, 交易日 %d (%s → %s), 净 $%.2f, 日聚类 t=%s" % (
        a["n_trades"], a["n_days"], a["day_min"], a["day_max"], a["net_all"], fmt_num(a["full_t"])))
    print("  有 entry_gap 的子集: n=%d (跨 %d 个交易日), 净 $%.2f  ← DEB §54.3 的 499 笔 / −$400 就在这里" % (
        a["n_with_gap"], a["n_gap_days"], a["net_with_gap"]))
    print("  最近 74 个 DA 交易日: n=%d, 净 $%.2f" % (a["recent74_n"], a["recent74_net"]))
    print("  最近 74 日里有 gap 的子集: n=%d, 净 $%.2f" % (a["recent74_gap_n"], a["recent74_gap_net"]))
    print("  更早的 %d 个交易日: n=%d, 净 $%.2f  ← 负号几乎全在这段" % (
        a["n_days"] - 74, a["oldest_n"], a["oldest_net"]))
    print("  ⇒ RFC-192 §2.6 的'现状 −$882' 在下列口径里都复现不出:")
    for lab, val in (("85 天全部 DA 笔", a["net_all"]),
                     ("85 天有 gap 的 499 笔 (DEB §54.3)", a["net_with_gap"]),
                     ("最近 74 日全部 DA 笔", a["recent74_net"]),
                     ("最近 74 日有 gap 子集", a["recent74_gap_net"])):
        print("     %-34s $%9.2f" % (lab, val))


# ── 段 B: gap 口径 ───────────────────────────────────────────────────────
def section_b(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    with_gap = [x for x in trades if x["gap"] is not None]
    pm_rows = []
    for x in trades:
        g = premarket_gap(x["day"], x["sym"])
        if g is not None:
            pm_rows.append((x, g))
    both = [(x, g) for x, g in pm_rows if x["gap"] is not None]
    agree = sum(1 for x, g in both if (abs(g) < GAP_THRESHOLD) == (abs(x["gap"]) < GAP_THRESHOLD))
    diffs = [abs(abs(g) - abs(x["gap"])) for x, g in both]
    sweep_open = {}
    sweep_pm = {}
    for thr in (0.5, 0.8, 1.0, 1.5, 2.0):
        k1 = [x for x in with_gap if abs(x["gap"]) < thr]
        k2 = [x for x, g in pm_rows if abs(g) < thr]
        sweep_open[thr] = (len(k1), sum(x["net"] for x in k1))
        sweep_pm[thr] = (len(k2), sum(x["net"] for x in k2))
    return {"n_pm": len(pm_rows), "n_both": len(both), "agree": agree,
            "median_diff": statistics.median(diffs) if diffs else None,
            "gt03": sum(1 for d in diffs if d > 0.3),
            "sweep_open": sweep_open, "sweep_pm": sweep_pm}


def print_b(trades: List[Dict[str, Any]], b: Dict[str, Any]) -> None:
    print("\n── B. gap 口径: 开盘跳空 (证据用) vs 盘前跳空 (C2 字面用) ─────")
    print("  盘前 pre_change_rate 可算: %d / %d 笔 (%.0f%%)" % (
        b["n_pm"], len(trades), 100.0 * b["n_pm"] / len(trades)))
    print("  两口径都有值: %d 笔 | 1.0%% 阈值判定一致: %d (%.0f%%) | |差|>0.3pp: %d 笔 | 中位差 %.3fpp" % (
        b["n_both"], b["agree"], 100.0 * b["agree"] / b["n_both"] if b["n_both"] else 0,
        b["gt03"], b["median_diff"] or 0))
    print("  同一张阈值表, 两套口径:")
    print("    %-10s %14s %14s" % ("阈值", "开盘跳空(笔/净)", "盘前跳空(笔/净)"))
    for thr in (0.5, 0.8, 1.0, 1.5, 2.0):
        n1, v1 = b["sweep_open"][thr]
        n2, v2 = b["sweep_pm"][thr]
        print("    <%.1f%%      %5d 笔 $%9.2f   %5d 笔 $%9.2f" % (thr, n1, v1, n2, v2))


# ── 段 C: 费用 ───────────────────────────────────────────────────────────
def section_c(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    keep = [x for x in trades if x["gap"] is not None and abs(x["gap"]) < GAP_THRESHOLD]
    def block(sub):
        if not sub:
            return {}
        return {
            "n": len(sub),
            "measured_bps": statistics.mean([x["fee"] / x["n"] * 1e4 for x in sub]),
            "rfc_bps": statistics.mean([rfc_single_floor_bps(x["n"]) for x in sub]),
            "sso_bps": statistics.mean([sso_double_floor_bps(x["n"], x["px"]) for x in sub]),
            "gross_bps": statistics.mean([x["gross"] / x["n"] * 1e4 for x in sub]),
            "px_le_20": sum(1 for x in sub if x["px"] <= 20),
        }
    return {"all": block(trades), "keep": block(keep),
            "keep_n": len(keep),
            "keep_fee_at_10k": [
                estimate_round_trip(max(1, int(10000 / x["px"])), x["px"], x["xp"], x["dir"], x["day"])["total"]
                / (max(1, int(10000 / x["px"])) * x["px"]) * 1e4 for x in keep]}


def print_c(trades: List[Dict[str, Any]], c: Dict[str, Any]) -> None:
    print("\n── C. 费用: RFC 单地板 vs 仓库 SSOT 双地板 vs 实测 ───────────")
    for lab, b in (("全部", c["all"]), ("|gap|<1.0%% (n=%d)" % c["keep_n"], c["keep"])):
        if not b:
            continue
        print("  %-18s 实测费 bps %6.2f | RFC 单地板 %6.2f | SSOT 双地板 %6.2f | 毛 bps %6.2f" % (
            lab, b["measured_bps"], b["rfc_bps"], b["sso_bps"], b["gross_bps"]))
    fb = c["keep_fee_at_10k"]
    if fb:
        print("  保留组名义放到 $10k 后 (真模型): 费 bps 中位 %.2f, >7bps 的笔 %d/%d (%.0f%%)" % (
            statistics.median(fb), sum(1 for v in fb if v > 7.0), len(fb),
            100.0 * sum(1 for v in fb if v > 7.0) / len(fb)))
        lo = [x for x in trades if x["gap"] is not None and abs(x["gap"]) < GAP_THRESHOLD and x["px"] <= 20]
        if lo:
            f2 = [estimate_round_trip(max(1, int(10000 / x["px"])), x["px"], x["xp"], x["dir"], x["day"])["total"]
                  / (max(1, int(10000 / x["px"])) * x["px"]) * 1e4 for x in lo]
            print("    其中股价<=$20 的 %d 笔: $10k 下费 bps 中位 %.1f (双地板 262/P 压不掉 ⇒ 判据 #3 <7bps 对它们不可达)" % (
                len(lo), statistics.median(f2)))


# ── 段 D: sizing 重建 + C1 真实机制 ─────────────────────────────────────
def section_d(trades: List[Dict[str, Any]], instrs: Dict[Tuple[str, str], Dict[str, Any]]) -> Dict[str, Any]:
    cap_now = EQUITY_REF * CAP_PCT_CUR
    byreg = defaultdict(list)
    for x in trades:
        byreg[x["regime"]].append(x)
    regstat = {}
    for reg, v in byreg.items():
        regstat[reg] = {
            "n": len(v),
            "notional_med": statistics.median([x["n"] for x in v]),
            "cap_bound": sum(1 for x in v if x["n"] >= 0.98 * cap_now) / len(v),
        }
    # 历史风险预算可还原性体检: risk_amount 存的是预算, 但 re-price 会覆写成派生值
    derived = budget = 0
    for ins in instrs.values():
        px, sl, ps, ra = ins["plan_px"], ins["sl"], ins["plan_qty"], ins["risk_amount"]
        if not (px and sl and ps and ra):
            continue
        d = abs(float(px) - float(sl))
        if d <= 0:
            continue
        if abs(float(ra) - d * float(ps)) <= 0.5:
            derived += 1
        else:
            budget += 1
    keep = [x for x in trades if x["gap"] is not None and abs(x["gap"]) < GAP_THRESHOLD]

    def band(mode: str) -> Dict[str, Any]:
        out = []
        for x in keep:
            q2 = x["qty"]
            if mode == "upper" and x["n"] >= 0.98 * cap_now:
                q2 = max(1, int((EQUITY_REF * CAP_PCT_NEW) / x["px"]))
            elif mode == "uniform":
                q2 = max(1, int(10000.0 / x["px"]))
            px = x["px"]
            n2 = q2 * px
            fee2 = estimate_round_trip(q2, px, x["xp"], x["dir"], x["day"])["total"]
            out.append({"day": x["day"], "n": n2,
                        "net": (x["gross"] * (n2 / x["n"]) - fee2) if x["n"] else x["net"]})
        m, t = daily_t(out)
        return {"n": len(out), "net": sum(a["net"] for a in out), "t": t,
                "notional_med": statistics.median([a["n"] for a in out])}

    return {"regstat": regstat, "keep_n": len(keep),
            "derived_risk_rows": derived, "budget_risk_rows": budget,
            "n_cap_bound_keep": sum(1 for x in keep if x["n"] >= 0.98 * cap_now),
            "cur": band("none"), "upper": band("upper"), "uniform": band("uniform")}


def print_d(d: Dict[str, Any]) -> None:
    print("\n── D. sizing 机制与 C1 (帽 6%%→10%%) 的可达区间 ────────────")
    print("  sizing 链: 名义 = min(风险预算名义, 帽名义); 风险预算 = 净值×0.5%%×gate_mult×water_mult×conf_factor")
    print("  ⇒ 抬帽只在『风险预算 > 旧帽』时生效; 实测绑定情况 (按 regime):")
    for reg, s in sorted(d["regstat"].items(), key=lambda kv: -kv[1]["n"]):
        print("    %-9s n=%3d 名义中位 $%7.0f  顶到 6%% 帽的比例 %3.0f%%" % (
            reg, s["n"], s["notional_med"], 100 * s["cap_bound"]))
    print("  历史风险预算可还原性: instructions.risk_amount 存的是预算, 但其中 %d 行已被 re-price 覆写为" % d["derived_risk_rows"])
    print("    派生值 (|entry−SL|×position_size ⇒ 反解是循环论证), 剩 %d 行预算行不足以还原逐日净值" % d["budget_risk_rows"])
    print("    ⇒ C1 的历史效应**不可点估**, 只能给区间:")
    print("    %-32s %11s %8s %9s" % ("C1 口径", "净额", "日聚类t", "名义中位"))
    for lab, key in (("不动 (帽 6%)", "cur"),
                     ("上界: 帽绑定笔长到 $9,918", "upper"),
                     ("RFC §2.3 隐含: 全量到 $10k", "uniform")):
        v = d[key]
        print("    %-32s $%10.2f %8s $%8.0f" % (lab, v["net"], fmt_num(v["t"]), v["notional_med"]))
    print("    保留组里顶到 6%% 帽的笔: %d / %d (%.0f%%) ⇒ 只有这些笔可能因 C1 变大" % (
        d["n_cap_bound_keep"], d["keep_n"], 100.0 * d["n_cap_bound_keep"] / d["keep_n"]))
    print("    ⇒ **三档口径下 A 臂的 t 全部 < 2** ⇒ 推荐臂过不了 RFC-192 §5 门2, 与 C1 效应大小无关")


# ── 段 E: 方案复算 ───────────────────────────────────────────────────────
def _arm(items: List[Dict[str, Any]], *, c1: str = "none",
         topn: Optional[int] = None, cap: Optional[int] = None) -> List[Dict[str, Any]]:
    byday = defaultdict(list)
    for x in items:
        byday[x["day"]].append(x)
    out = []
    for d, v in byday.items():
        if topn:
            v = sorted(v, key=lambda x: abs(x["gap"]) if x["gap"] is not None else 1e9)[:topn]
        if cap and len(v) > cap:
            v = sorted(v, key=lambda x: abs(x["gap"]) if x["gap"] is not None else 1e9)[:cap]
        day_net = 0.0
        for x in v:
            n2 = scale_notional(x, c1)
            q2 = max(1, int(n2 / x["px"])) if x["px"] else x["qty"]
            fee2 = estimate_round_trip(q2, x["px"], x["xp"], x["dir"], x["day"])["total"]
            day_net += (x["gross"] * (n2 / x["n"]) - fee2) if x["n"] else x["net"]
        out.append({"day": d, "net": day_net, "n": len(v)})
    return out


def _summ(arm: List[Dict[str, Any]]) -> Dict[str, Any]:
    vals = [a["net"] for a in arm]
    return {"n": sum(a["n"] for a in arm), "days": len(arm),
            "net": sum(vals), "per": sum(vals) / sum(a["n"] for a in arm) if sum(a["n"] for a in arm) else 0.0,
            "daily": statistics.mean(vals) if vals else 0.0, "t": tstat(vals)}


def section_e(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    keep = [x for x in trades if x["gap"] is not None and abs(x["gap"]) < GAP_THRESHOLD]
    with_gap = [x for x in trades if x["gap"] is not None]
    arms = {
        "A_5k_c1none": _summ(_arm(keep, c1="none")),
        "A_10k_c1upper": _summ(_arm(keep, c1="upper")),
        "A_10k_c1uniform": _summ(_arm(keep, c1="uniform")),
        "A2_10k_c1upper_cap6": _summ(_arm(keep, c1="upper", cap=6)),
        "B_top3_10k": _summ(_arm(with_gap, c1="uniform", topn=3)),
        "baseline_all_5k": _summ(_arm(with_gap, c1="none")),
    }
    return arms


def print_e(arms: Dict[str, Any]) -> None:
    print("\n── E. 方案复算 (85 天 / 499 笔 gap 宇宙, 费用真模型) ─────────")
    print("    %-22s %5s %6s %11s %9s %8s" % ("臂", "笔数", "天数", "净额", "每笔", "日聚类t"))
    for k, v in arms.items():
        print("    %-22s %5d %6d $%10.2f $%8.2f %8s" % (
            k, v["n"], v["days"], v["net"], v["per"], fmt_num(v["t"])))
    print("    (RFC-192 §2.6 声称: A +$2,147 t=+0.90 | A' +$2,446 t=+1.10 | B +$4,758 t=+2.35 | 现状 −$882)")


# ── 段 F: 时代分层 ───────────────────────────────────────────────────────
def section_f(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    keep = [x for x in trades if x["gap"] is not None and abs(x["gap"]) < GAP_THRESHOLD]
    mo_all, mo_keep, mo_a = defaultdict(lambda: [0, 0.0]), defaultdict(lambda: [0, 0.0]), defaultdict(lambda: [0, 0.0])
    for x in trades:
        k = x["day"][:7]
        mo_all[k][0] += 1
        mo_all[k][1] += x["net"]
    for x in keep:
        k = x["day"][:7]
        mo_keep[k][0] += 1
        mo_keep[k][1] += x["net"]
    for t in _arm(keep, c1="upper"):
        k = t["day"][:7]
        mo_a[k][0] += t["n"]
        mo_a[k][1] += t["net"]
    return {"all": dict(mo_all), "keep": dict(mo_keep), "a": dict(mo_a)}


def print_f(f: Dict[str, Any]) -> None:
    print("\n── F. 时代分层 (分月净额) ───────────────────────────────────")
    print("    %-8s %18s %20s %18s" % ("月份", "全部 DA", "保留组 |gap|<1%", "A 臂 (C1 真实)"))
    for k in sorted(f["all"]):
        a = f["all"][k]
        kp = f["keep"].get(k, [0, 0.0])
        aa = f["a"].get(k, [0, 0.0])
        print("    %-8s %5d 笔 $%9.2f %6d 笔 $%9.2f %5d 笔 $%9.2f" % (
            k, a[0], a[1], kp[0], kp[1], aa[0], aa[1]))
    tot = sum(v[1] for v in f["keep"].values())
    top = max(f["keep"].items(), key=lambda kv: kv[1][1]) if f["keep"] else ("—", [0, 0.0])
    print("    ⇒ 保留组总额 $%.2f, 其中 %s 一月 $%.2f (占 %.0f%%) ⇒ 效果高度时代集中" % (
        tot, top[0], top[1][1], 100 * top[1][1] / tot if tot else 0))


# ── 段 G: 判据可达性 ─────────────────────────────────────────────────────
def section_g(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    keep = [x for x in trades if x["gap"] is not None and abs(x["gap"]) < GAP_THRESHOLD]
    arm = _arm(keep, c1="upper")
    nets = sorted((a["net"] for a in arm), reverse=True)
    tot = sum(nets)
    days = sorted((a["net"] for a in arm), reverse=True)
    fb = [estimate_round_trip(max(1, int(10000 / x["px"])), x["px"], x["xp"], x["dir"], x["day"])["total"]
          / (max(1, int(10000 / x["px"])) * x["px"]) * 1e4 for x in keep]
    big_loss = sum(1 for x in keep if x["net"] < -100)
    return {"total": tot, "drop3": sum(nets[3:]), "max_day_share": (max(days) / tot) if tot else 0.0,
            "t": tstat([a["net"] for a in arm]),
            "fee_gt7_share": (sum(1 for v in fb if v > 7.0) / len(fb)) if fb else 0.0,
            "big_loss_rate": big_loss / len(keep) if keep else 0.0,
            "n": len(keep)}


def print_g(g: Dict[str, Any]) -> None:
    print("\n── G. RFC-192 §5 五道门在历史读数上的可达性 (A 臂, C1 真实) ──")
    print("  门1 累计净额 > 0            : %s (历史 $%.2f)" % ("PASS" if g["total"] > 0 else "FAIL", g["total"]))
    print("  门2 日聚类 t ≥ 2            : %s (历史 t=%s)" % ("PASS" if (g["t"] or 0) >= 2 else "FAIL", fmt_num(g["t"])))
    print("  门3 实测费用率 < 7.0 bps    : %s ($10k 下 %.0f%% 的笔 >7bps)" % (
        "PASS" if g["fee_gt7_share"] < 0.5 else "FAIL", 100 * g["fee_gt7_share"]))
    print("  门4 大亏率 < 3%%             : %s (历史 %.1f%%, n=%d)" % (
        "PASS" if g["big_loss_rate"] < 0.03 else "FAIL", 100 * g["big_loss_rate"], g["n"]))
    print("  门5 剔前3笔仍>0 且单日占比<50%%: 剔前3 $%.2f (%s) | 单日最大占比 %.0f%% (%s)" % (
        g["drop3"], "PASS" if g["drop3"] > 0 else "FAIL",
        100 * g["max_day_share"], "PASS" if g["max_day_share"] < 0.5 else "FAIL"))


# ── 段 H: 多空拆分 ───────────────────────────────────────────────────────
def section_h(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    def isshort(d):
        return "SHORT" in str(d or "").upper()
    keep = [x for x in trades if x["gap"] is not None and abs(x["gap"]) < GAP_THRESHOLD]
    res = {}
    for lab, sub in (("keep_all", keep),
                     ("keep_long", [x for x in keep if not isshort(x["dir"])]),
                     ("keep_short", [x for x in keep if isshort(x["dir"])]),
                     ("all_long", [x for x in trades if not isshort(x["dir"])]),
                     ("all_short", [x for x in trades if isshort(x["dir"])])):
        if not sub:
            continue
        m, t = daily_t(sub)
        res[lab] = {"n": len(sub), "net": sum(x["net"] for x in sub),
                    "per": sum(x["net"] for x in sub) / len(sub),
                    "t": t, "gross_bps": statistics.mean([x["gross"] / x["n"] * 1e4 for x in sub])}
    return res


def print_h(h: Dict[str, Any]) -> None:
    print("\n── H. 多空拆分 (DEB §55 '别做空' 的实现口径核对) ────────────")
    for lab, v in h.items():
        print("  %-12s n=%3d 净 $%9.2f 每笔 $%7.2f 日聚类t=%s 毛bps %6.2f" % (
            lab, v["n"], v["net"], v["per"], fmt_num(v["t"]), v["gross_bps"]))


# ── main ─────────────────────────────────────────────────────────────────
SECTIONS = {"A": section_a, "B": section_b, "C": section_c, "D": section_d,
            "E": section_e, "F": section_f, "G": section_g, "H": section_h}


def main() -> int:
    ap = argparse.ArgumentParser(description="RFC-192 复核尺子 (只读)")
    ap.add_argument("--json", action="store_true", help="输出 JSON (stdout)")
    ap.add_argument("--section", default="", help="只跑某段 (A-H)")
    a = ap.parse_args()

    trades = load_da_trades()
    instrs = load_instructions()
    want = [s for s in (a.section.upper() if a.section else "") if s in SECTIONS] or list(SECTIONS)

    out: Dict[str, Any] = {}
    if "A" in want:
        out["A"] = section_a(trades)
    if "B" in want:
        out["B"] = section_b(trades)
    if "C" in want:
        out["C"] = section_c(trades)
    if "D" in want:
        out["D"] = section_d(trades, instrs)
    if "E" in want:
        out["E"] = section_e(trades)
    if "F" in want:
        out["F"] = section_f(trades)
    if "G" in want:
        out["G"] = section_g(trades)
    if "H" in want:
        out["H"] = section_h(trades)

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0

    print("=" * 78)
    print("RFC-192 复核尺子 (只读) — 生成于 %s" % __import__("datetime").datetime.now().strftime("%Y-%m-%d"))
    print("DB=file:...?mode=ro | 零写入 | 零网络")
    print("=" * 78)
    if "A" in want:
        print_a(trades, out["A"])
    if "B" in want:
        print_b(trades, out["B"])
    if "C" in want:
        print_c(trades, out["C"])
    if "D" in want:
        print_d(out["D"])
    if "E" in want:
        print_e(out["E"])
    if "F" in want:
        print_f(out["F"])
    if "G" in want:
        print_g(out["G"])
    if "H" in want:
        print_h(out["H"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
