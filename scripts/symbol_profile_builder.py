#!/usr/bin/env python3
"""个股画像 L0 构建器 (symbol-profile P0, 只读)。

理念: 「盘感」= 记账(trade_records) + 复盘(daily_reviews) + 摸股性(本脚本)。
本脚本聚合个股级 L0 事实层 (纯代码计算, 零 LLM, 零幻觉):

  interaction   交互史: 笔数/胜率/盈亏分布/方向拆分/入场时段矩阵/退出原因/利润捕获率
  path_behavior 路径行为 (持仓路径湖, 2026-07-23 起):
    - mfe_arrival   入场后多久见 MFE (快枪手 vs 慢热)
    - sl_rebound    止损后 30min 回升 (假止损/洗盘倾向)
    - trail_premature 拖尾退出后延续 (跑早了倾向)

落盘: data/symbol_profiles/{symbol}.json (schema symbol_profile/L0)
安全: 只读 DB (mode=ro) + 只读路径湖, 不触任何 live 链路。
用法: PYTHONPATH=. ./venv/bin/python scripts/symbol_profile_builder.py \
        [--symbols US.QCOM,US.ORCL] [--top 10] [--no-json]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics as st
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))  # [RFC-070] utils.et_time
DB = REPO / "data" / "ftseek_us.db"
PATH_DIR = REPO / "data" / "position_paths"
OUT_DIR = REPO / "data" / "symbol_profiles"

SL_PAT = re.compile(r"sl", re.I)
TRAIL_REASONS = {"TRAILING_STOP", "SL_TRAIL_LOCK", "MFE_STALL_EXIT"}
CLOCK_REASONS = {"TIME_EXIT", "PRE_CLOSE_EXIT", "EOD_FORCE_CLOSE"}


# ── 工具 ─────────────────────────────────────────────────────────
def _parse_et(s) -> datetime | None:
    """库内时间统一 naive ET; 残留 '+08:00' 后缀按历史口径直接剥除 (与 path_collector 一致)."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("+08:00", "")).replace(tzinfo=None)
    except ValueError:
        return None


# 旧引擎 (MAIN_PIPELINE/SCALPING) entry_time 为北京时间, PLAYBOOK 系为 ET (时区家族坑, 2026-08-17 实证)
LEGACY_BEIJING = {"MAIN_PIPELINE", "SCALPING"}


def _entry_time_et(r) -> datetime | None:
    t = _parse_et(r.get("entry_time"))
    if t and (r.get("source_type") or "") in LEGACY_BEIJING:
        # [RFC-070] 北京→ET DST 正确 (EST -13h, 原手写 -12h 收编; "样本期全 EDT" 注释即冬令时炸弹)
        from utils.et_time import naive_cst_to_et
        return naive_cst_to_et(t)
    return t


def _parse_bar_t(s) -> datetime | None:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _med(xs, nd=2):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), nd) if xs else None


def _pctl(xs, q, nd=2):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    i = min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))
    return round(xs[i], nd)


def _pnl_of(r) -> float | None:
    """净额优先, 老数据回退 realized_pnl."""
    return r["pnl_net"] if r.get("pnl_net") is not None else r.get("realized_pnl")


def _px_pct_of(r) -> float | None:
    """方向调整后的价格收益率(百分数), 与 mfe_pct 同口径; 规避 pnl_pct 小数/百分数历史混存坑."""
    if r.get("entry_price") and r.get("exit_price"):
        sign = 1 if r.get("direction") == "LONG" else -1
        return round(sign * (r["exit_price"] / r["entry_price"] - 1) * 100, 4)
    return None


# ── 数据加载 ──────────────────────────────────────────────────────
def load_trades(symbol: str) -> list[dict]:
    # 交互史 = 全部真实闭环操作 (含 MAIN_PIPELINE/SCALPING 旧引擎), source_type 拆分见下
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT * FROM trade_records WHERE symbol = ? AND exit_time IS NOT NULL "
        "AND exit_price > 0 ORDER BY trade_date",
        (symbol,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def load_paths(symbol: str) -> list[dict]:
    out = []
    if not PATH_DIR.exists():
        return out
    for fp in sorted(PATH_DIR.glob("*.json")):
        try:
            d = json.loads(fp.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("symbol") == symbol and d.get("bars"):
            out.append(d)
    out.sort(key=lambda d: d.get("trade_date") or "")
    return out


# ── L0: 交互史 ────────────────────────────────────────────────────
def build_interaction(trades: list[dict]) -> dict:
    pnls = [p for p in (_pnl_of(r) for r in trades) if p is not None]
    winners = [r for r in trades if (_pnl_of(r) or 0) > 0]

    def bucket(rs):
        pn = [p for p in (_pnl_of(r) for r in rs) if p is not None]
        return {"n": len(rs),
                "win_rate": round(sum(1 for p in pn if p > 0) / len(pn), 3) if pn else None,
                "avg_pnl": round(st.mean(pn), 2) if pn else None}

    # 入场时段矩阵 (entry_session 列 64% 为空, 自行从 entry_time 推导 ET 小时)
    hours = {}
    for r in trades:
        t = _entry_time_et(r)
        if not t or not (9 <= t.hour <= 15):
            continue
        hours.setdefault(t.hour, []).append(r)
    hour_matrix = {f"{h:02d}:00": bucket(rs) for h, rs in sorted(hours.items())}

    reasons = {}
    for r in trades:
        reasons[r.get("exit_reason") or "UNKNOWN"] = reasons.get(r.get("exit_reason") or "UNKNOWN", 0) + 1
    top_reasons = sorted(reasons.items(), key=lambda kv: -kv[1])[:6]

    cap = []  # 利润捕获率: 盈利单价格收益 / MFE 幅度 (统一价格百分比口径)
    for r in winners:
        pp, mfe = _px_pct_of(r), r.get("mfe_pct")
        if pp is not None and mfe and mfe > 0.05:
            cap.append(min(abs(pp) / mfe, 2.0))

    hold = [r["holding_hours"] for r in trades if r.get("holding_hours")]

    return {
        "n_trades": len(trades),
        "date_range": [trades[0]["trade_date"], trades[-1]["trade_date"]] if trades else None,
        "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls), 3) if pnls else None,
        "pnl": {"sum": round(sum(pnls), 2), "avg": round(st.mean(pnls), 2),
                "median": _med(pnls), "best": round(max(pnls), 2) if pnls else None,
                "worst": round(min(pnls), 2) if pnls else None},
        "direction_split": {d: bucket([r for r in trades if r.get("direction") == d])
                            for d in ("LONG", "SHORT")
                            if any(r.get("direction") == d for r in trades)},
        "source_type_split": {s: bucket([r for r in trades if (r.get("source_type") or "UNKNOWN") == s])
                              for s in sorted({r.get("source_type") or "UNKNOWN" for r in trades})},
        "entry_hour_matrix": hour_matrix,
        "exit_reason_top": [{"reason": k, "n": v, "pct": round(v / len(trades), 3)}
                            for k, v in top_reasons],
        "holding_hours": {"median": _med(hold), "p75": _pctl(hold, 0.75)},
        "mfe_pct_median": _med([r.get("mfe_pct") for r in trades if (r.get("mfe_pct") or 0) > 0]),
        "mae_pct_median": _med([r.get("mae_pct") for r in trades if (r.get("mae_pct") or 0) > 0]),
        "profit_capture_median": _med(cap, 3),  # 盈利单 pnl/MFE, >1 说明亏损单见好就收极快
    }


# ── L0: 路径行为 (持仓路径湖) ─────────────────────────────────────
def _bars_after(bars, t_exit):
    return [b for b in bars if _parse_bar_t(b[0]) and _parse_bar_t(b[0]) > t_exit]


def build_path_behavior(paths: list[dict]) -> dict:
    arrivals, sl_stats, trail_stats = [], [], []
    for d in paths:
        t_entry, t_exit = _parse_et(d.get("entry_time")), _parse_et(d.get("exit_time"))
        if not t_entry or not t_exit or not d.get("bars"):
            continue
        bars = [( _parse_bar_t(b[0]), b[1], b[2], b[3], b[4]) for b in d["bars"]]
        bars = [b for b in bars if b[0]]
        in_win = [b for b in bars if t_entry <= b[0] <= t_exit]
        after = [b for b in bars if b[0] > t_exit]
        long = d.get("direction") == "LONG"

        # MFE 到达时间: 入场→出场窗口内最优价 bar 距入场分钟数
        if in_win:
            best = max(in_win, key=lambda b: b[2]) if long else min(in_win, key=lambda b: b[3])
            arrivals.append((best[0] - t_entry).total_seconds() / 60.0)

        reason = d.get("exit_reason") or ""
        exit_px, entry_px = d.get("exit_price"), d.get("entry_price")
        if SL_PAT.search(reason) and exit_px and entry_px and after:
            ext = max(b[2] for b in after) if long else min(b[3] for b in after)
            sl_stats.append({
                "reason": reason,
                # 回到入场价有利侧 = 止损可能打在洗盘地毯价 (假止损信号)
                "reback": (ext >= entry_px) if long else (ext <= entry_px),
                "rebound_pct": (ext / exit_px - 1) * (1 if long else -1) * 100,
            })
        if reason in TRAIL_REASONS and exit_px and after:
            ext = max(b[2] for b in after) if long else min(b[3] for b in after)
            trail_stats.append({
                "reason": reason,
                # 退出后 30min 窗口内继续朝有利方向走 >0.3% = 跑早了信号
                "continued": ((ext / exit_px - 1) * (1 if long else -1) * 100) > 0.3,
                "cont_pct": (ext / exit_px - 1) * (1 if long else -1) * 100,
            })

    n_sl, n_tr = len(sl_stats), len(trail_stats)
    return {
        "n_paths": len(paths),
        "path_date_range": [paths[0]["trade_date"], paths[-1]["trade_date"]] if paths else None,
        "mfe_arrival_minutes": {
            "median": _med(arrivals, 1), "p25": _pctl(arrivals, 0.25, 1),
            "p75": _pctl(arrivals, 0.75, 1), "n": len(arrivals)},
        "sl_rebound": {
            "n_sl_exits": n_sl,
            "reback_to_entry_rate": round(sum(1 for s in sl_stats if s["reback"]) / n_sl, 3) if n_sl else None,
            "median_max_rebound_pct": _med([s["rebound_pct"] for s in sl_stats], 2),
        },
        "trail_premature": {
            "n_trail_exits": n_tr,
            "continuation_rate": round(sum(1 for s in trail_stats if s["continued"]) / n_tr, 3) if n_tr else None,
            "median_continuation_pct": _med([s["cont_pct"] for s in trail_stats], 2),
        },
    }


# ── 报告 ─────────────────────────────────────────────────────────
def render(symbol: str, prof: dict) -> str:
    it, pb = prof["interaction"], prof["path_behavior"]
    L = [f"\n{'=' * 62}", f"◆ {symbol}  (L0 画像, 闭环 {it['n_trades']} 笔, {it['date_range'][0]} → {it['date_range'][1]})",
         f"  胜率 {it['win_rate']} | PnL 累计 {it['pnl']['sum']} (中位 {it['pnl']['median']}, 最差 {it['pnl']['worst']})"]
    for d, s in (it.get("direction_split") or {}).items():
        L.append(f"  {d:<5} n={s['n']:<3} 胜率 {s['win_rate']}  均值 {s['avg_pnl']}")
    for s, v in (it.get("source_type_split") or {}).items():
        L.append(f"  [{s}] n={v['n']:<3} 胜率 {v['win_rate']}  均值 {v['avg_pnl']}")
    def _fmt(v, suf=""):
        return f"{v}{suf}" if v is not None else "NA"
    L.append(f"  持仓中位 {_fmt(it['holding_hours']['median'], 'h')} | MFE中位 {_fmt(it['mfe_pct_median'], '%')} | "
             f"MAE中位 {_fmt(it['mae_pct_median'], '%')} | 利润捕获率 {_fmt(it['profit_capture_median'])}")
    L.append("  退出Top: " + ", ".join(f"{r['reason']} {r['pct']:.0%}" for r in it["exit_reason_top"]))
    hm = it["entry_hour_matrix"]
    if hm:
        cells = [f"{k} {v['win_rate'] if v['win_rate'] is not None else '-'}({v['n']})" for k, v in hm.items()]
        L.append("  入场时段(胜率(笔)): " + " | ".join(cells))
    L.append(f"  ── 路径行为 (样本 {pb['n_paths']} 笔, {pb['path_date_range'][0] if pb['path_date_range'] else '-'} → "
             f"{pb['path_date_range'][1] if pb['path_date_range'] else '-'})")
    ma = pb["mfe_arrival_minutes"]
    if ma["n"]:
        L.append(f"  MFE到达: 中位 {ma['median']}min [p25 {ma['p25']}, p75 {ma['p75']}] (n={ma['n']})")
    else:
        L.append("  MFE到达: 样本不足")
    sr, tp_ = pb["sl_rebound"], pb["trail_premature"]
    if sr["n_sl_exits"]:
        L.append(f"  止损后回升: 回到入场价比例 {sr['reback_to_entry_rate']} | 最大回升中位 {sr['median_max_rebound_pct']}% "
                 f"(n={sr['n_sl_exits']}) ← 假止损/洗盘倾向")
    else:
        L.append("  止损后回升: 样本不足")
    if tp_["n_trail_exits"]:
        L.append(f"  拖尾跑早率: 退出后延续比例 {tp_['continuation_rate']} | 延续中位 {tp_['median_continuation_pct']}% "
                 f"(n={tp_['n_trail_exits']})")
    else:
        L.append("  拖尾跑早率: 样本不足")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="个股 L0 画像构建器 (只读)")
    ap.add_argument("--symbols", help="逗号分隔, 缺省取闭环笔数 top N")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--no-json", action="store_true")
    args = ap.parse_args()

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = c.execute(
            "SELECT symbol, COUNT(*) n FROM trade_records WHERE exit_time IS NOT NULL "
            "GROUP BY symbol ORDER BY n DESC LIMIT ?",
            (args.top,)).fetchall()
        c.close()
        symbols = [r[0] for r in rows]

    if not args.no_json:
        OUT_DIR.mkdir(parents=True, exist_ok=True)

    for sym in symbols:
        trades = load_trades(sym)
        if not trades:
            print(f"[skip] {sym} 无闭环交易")
            continue
        paths = load_paths(sym)
        prof = {
            "schema": "symbol_profile/L0@0.1",
            "symbol": sym,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "interaction": build_interaction(trades),
            "path_behavior": build_path_behavior(paths),
            "caveats": [
                "interaction 含旧引擎交易 (MAIN_PIPELINE/SCALPING, 参数体系不同), 见 source_type_split",
                "旧引擎 entry_time 为北京时间, 已按 -12h 转 ET (夏令时口径, 样本期 5-8 月全为 EDT)",
                "path_behavior 仅 PLAYBOOK 口径且只覆盖 2026-07-23 起平仓笔 (路径湖物理边界)",
                "pnl 口径: pnl_net 优先, 老数据回退 realized_pnl; pnl_pct 历史小数/百分数混存, 捕获率已改价格口径",
                "mfe/mae 仅统计 >0 值 (旧引擎 253 行零值不回填)",
            ],
        }
        print(render(sym, prof))
        if not args.no_json:
            fp = OUT_DIR / f"{sym.replace('.', '_')}.json"
            fp.write_text(json.dumps(prof, ensure_ascii=False, indent=2))
            print(f"  → {fp.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
