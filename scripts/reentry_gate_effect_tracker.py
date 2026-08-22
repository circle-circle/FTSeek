#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
盘中再入场证伪门效果量化追踪器 (2026-08-22, 用户裁"做好记录和逻辑统计")
========================================================================
目的: 量化 9 道证伪门接通后, 盘中再入场的胜率与收益, 验证"是否高概率获益",
     并为"复用到 DA 盘中交易验真"提供可毕业的证据链。

方法 (两层):
  A. 历史一次性回放 (--backfill): 对 22 笔历史武装候选, 用历史数据重建各门
     判定 (可回放的门: 资金流分钟级/财报日/技术指标; 无历史源的门标记
     "无历史数据"按 fail-open 放行并如实披露), 配真实/反事实结局。
  B. 每日增量 (--daily, cron 07:20 CST): 前一个交易日的候选, 按门接通后的
     真实判定落盘 (无需重建), 攒前向样本。

结局口径 (预注册, 见数前定):
  - 成交单: trade_records 真实 pnl_net (费用已扣)。
  - 未成交单: replay_bars 反事实 — 武装时刻价格入场, 当日最后一根 bar 收盘
    出场 (与再入场"当日了结"的实际行为一致), 扣 13bps 往返费用。
    方向处理: BUY=exit-entry, SHORT=entry-exit。
  - 胜率 = 净盈亏>0 笔数 / 总笔数。

毕业判据 (预注册, 与 eval_intraday_reentry.py 一致):
  n≥30 且 净均值>0 且 优于被拒组反事实 → 才谈"有效可复用"。
  n<30 一切结论仅方向性参考, 禁止据此扩大权限。

只读: 零交易路径改动。输出:
  data/reentry_gate_effect_history.jsonl  (每笔候选一行, 含门判定+结局)
  data/reentry_gate_effect_report.json    (汇总)
回滚: 删本脚本 + 上述两个 data 文件 + cron 行。
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SHADOW = BASE / "data" / "intraday_reentry_shadow.jsonl"
DB = BASE / "data" / "ftseek_us.db"
SNAP_DB = BASE / "data" / "signal_snapshots.db"
BARS_DIR = BASE / "data" / "replay_bars"
CF_FILE = BASE / "data" / "futu_capital_flow.jsonl"
OUT_HISTORY = BASE / "data" / "reentry_gate_effect_history.jsonl"
OUT_REPORT = BASE / "data" / "reentry_gate_effect_report.json"
FEE_BPS = 13.0  # 往返费用估计


# ── 数据加载 ──────────────────────────────────────────────────────────────

def load_armed_candidates():
    """全部武装候选 (含未成交的, 它们也要算反事实)。"""
    out = []
    for line in open(SHADOW, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("action") == "armed":
            out.append(d)
    return out


def load_capital_flow_index():
    """(date, symbol) -> 武装前最后一条大单净流方向。分钟级, 只认当日。"""
    idx = defaultdict(list)
    if not CF_FILE.exists():
        return idx
    for line in open(CF_FILE, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        day = str(d.get("item_time", ""))[:10]
        idx[(day, d.get("symbol"))].append(
            (str(d.get("item_time", "")),
             float(d.get("super_in_flow", 0) or 0) + float(d.get("big_in_flow", 0) or 0)))
    return idx


def load_fills():
    """(date, symbol) -> [当日全部成交笔] (归因须比对入场时刻与方向)。"""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = con.execute("""
        SELECT date(entry_time) AS d, symbol, entry_price, exit_price,
               pnl_net, realized_pnl, exit_reason, entry_time, direction
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')
        ORDER BY entry_time
    """).fetchall()
    con.close()
    out = defaultdict(list)
    for r in rows:
        out[(r["d"], r["symbol"])].append(dict(r))
    return out


def load_bars(sym, day):
    """replay_bars: list of [ts, open, high, low, close]。"""
    p = BARS_DIR / f"US_{sym.replace('US.', '')}_{day}.json"
    if not p.exists():
        return None
    try:
        return json.load(open(p))
    except Exception:
        return None


def load_earnings_dates():
    """实时查财报日历 (report_date 是静态事实, 追溯查询有效)。"""
    try:
        sys.path.insert(0, str(BASE))
        from app.data.earnings_calendar import EarningsCalendar
        ec = EarningsCalendar()
        rows = ec.get_watchlist_earnings() or []
        return {r.get("symbol"): r.get("report_date") for r in rows if isinstance(r, dict)}
    except Exception:
        return {}


# ── 门判定重建 (仅历史可回放的门) ────────────────────────────────────────

def gate_verdicts_backfill(cand, cf_idx, earnings_dates, arm_hhmm):
    """返回 {门: True=会拦 / False=放行 / None=无历史数据}。"""
    sym, day, direction = cand["symbol"], cand["date"], cand["direction"]
    is_long = direction.upper() in ("BUY", "LONG")
    v = {}

    # 1. 资金流背离: 武装时刻之前最后一条当日大单净流
    flows = cf_idx.get((day, sym))
    if flows is None:
        v["capital_flow_divergence"] = None
    else:
        arm_ts_prefix = f"{day} {arm_hhmm[:2]}:{arm_hhmm[2:]}"
        prior = [f for f in flows if f[0] <= arm_ts_prefix]
        if not prior:
            v["capital_flow_divergence"] = None
        else:
            large = prior[-1][1]
            if large > 0 and not is_long:
                v["capital_flow_divergence"] = True
            elif large < 0 and is_long:
                v["capital_flow_divergence"] = True
            else:
                v["capital_flow_divergence"] = False

    # 2. 财报临近: report_date 距 armed 日 <=3 交易日 (粗用日历日, 保守)
    rd = earnings_dates.get(sym)
    if rd:
        try:
            delta = (datetime.strptime(rd, "%Y-%m-%d") -
                     datetime.strptime(day, "%Y-%m-%d")).days
            v["earnings_proximity"] = (0 <= delta <= 4)
        except Exception:
            v["earnings_proximity"] = None
    else:
        v["earnings_proximity"] = False  # 日历无此票 = 近期无财报

    # 3-8: 无历史数据源的门 — 按线上语义 (缺失→放行) 标记
    for gate in ["short_squeeze", "insider_divergence", "analyst_divergence",
                 "sentiment_extreme", "sec_event", "ticker_sentiment"]:
        v[gate] = None
    v["social_sentiment"] = False  # 设计性关断
    return v


# ── 结局计算 ─────────────────────────────────────────────────────────────

def outcome(cand, fills):
    """(filled, pnl_net, source)。归因: 成交必须入场时刻晚于武装时刻且方向一致,
    否则是"前笔"(触发候选的那笔)而非再入场单 → 走反事实。"""
    key = (cand["date"], cand["symbol"])
    arm_ts = str(cand.get("ts_et", ""))[:19]
    want_long = str(cand["direction"]).upper() in ("BUY", "LONG")
    for fill in fills.get(key, []):
        # 入场时刻晚于武装 + 方向一致 → 才是再入场成交
        if str(fill["entry_time"])[:19] > arm_ts and \
                (str(fill["direction"]).upper() == "LONG") == want_long:
            net = fill.get("pnl_net")
            if net is not None:
                return True, float(net), "real_fill"
            if fill.get("realized_pnl") is not None:
                return True, float(fill["realized_pnl"]), "real_fill_gross"

    # 反事实: 武装时刻价格入场 → 当日最后一根 bar 收盘
    bars = load_bars(cand["symbol"], cand["date"])
    if not bars:
        return False, None, "unmeasurable"
    entry = float(cand.get("price") or 0)
    if entry <= 0:
        return False, None, "unmeasurable"
    exit_px = float(bars[-1][4])
    is_long = cand["direction"].upper() in ("BUY", "LONG")
    raw_pct = (exit_px - entry) / entry if is_long else (entry - exit_px) / entry
    fee = FEE_BPS / 10000
    qty = 1
    # 用 DA 常规单名帽 ~$5800 估名义, 得到美元口径
    notional = 5800.0
    qty = int(notional / entry) if entry > 0 else 0
    pnl = (exit_px - entry) * qty if is_long else (entry - exit_px) * qty
    pnl_net = pnl - notional * fee
    return False, round(pnl_net, 2), "counterfactual_bars"


# ── 主流程 ───────────────────────────────────────────────────────────────

def main():
    armed = load_armed_candidates()
    fills = load_fills()
    cf_idx = load_capital_flow_index()
    earnings_dates = load_earnings_dates()
    print(f"武装候选 {len(armed)} | 真实成交 {sum(1 for a in armed if (a['date'],a['symbol']) in fills)}")

    records = []
    for cand in armed:
        hhmm = str(cand.get("decision_id", "")).split("-")[-1] if "decision_id" in cand else ""
        if not hhmm:
            # decision_id 在 armed 行的影子里没有, 从 instruction 表取
            pass
        # 从影子行拿武装时刻 (ts_et)
        arm_hhmm = cand.get("ts_et", "")[11:16].replace(":", "") if cand.get("ts_et") else "1200"
        verdicts = gate_verdicts_backfill(cand, cf_idx, earnings_dates, arm_hhmm)
        blocked = [g for g, b in verdicts.items() if b is True]
        filled, pnl, src = outcome(cand, fills)
        records.append({
            "date": cand["date"], "symbol": cand["symbol"],
            "direction": cand["direction"], "arm_price": cand.get("price"),
            "prior_exit_reason": cand.get("prior_exit_reason"),
            "gate_verdicts": verdicts,
            "would_block": bool(blocked), "blocking_gates": blocked,
            "filled": filled, "pnl_net": pnl, "outcome_source": src,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        })

    # 落盘历史
    with open(OUT_HISTORY, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 汇总报告
    meas = [r for r in records if r["pnl_net"] is not None]
    blocked_set = [r for r in meas if r["would_block"]]
    pass_set = [r for r in meas if not r["would_block"]]

    def stat(rs):
        if not rs:
            return {"n": 0}
        pnls = [r["pnl_net"] for r in rs]
        wins = sum(1 for p in pnls if p > 0)
        return {
            "n": len(rs), "wins": wins,
            "win_rate": round(wins / len(rs), 3),
            "sum_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
        }

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "method": "历史回放: 资金流+财报门重建, 其余门无历史源按fail-open; 未成交用replay_bars反事实(扣13bps)",
        "total_armed": len(records),
        "all_measurable": stat(meas),
        "gate_would_block": stat(blocked_set),
        "gate_pass": stat(pass_set),
        "blocking_detail": [
            {"date": r["date"], "symbol": r["symbol"], "gates": r["blocking_gates"],
             "cf_pnl": r["pnl_net"]} for r in blocked_set
        ],
        "graduation_check": {
            "criteria": "n≥30 且净均值>0 且优于被拒组反事实",
            "status": ("样本不足, 仅方向性参考" if len(meas) < 30 else "见数"),
            "n": len(meas),
        },
    }
    with open(OUT_REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
