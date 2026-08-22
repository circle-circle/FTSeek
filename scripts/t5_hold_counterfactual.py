#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T+5 摆动变体离线反事实回放 (只读, mode=ro) — DEB-20260822 任务A。

背景
----
方向×周期矩阵已证 BUY T+1 连续 5 周低于掷币(反指) 而 T+5 正指 (direction_counterfactual_ruler),
隔夜影子亦显示"持到次日收盘"优于实际出场。本工具把该假设落到**逐笔净额层面**:
对干净窗口 PLAYBOOK 已平仓交易, 反事实"持有至 T+5 收盘"(忽略一切提前出场),
用同一账本费用口径配对比较 实际净额 vs T+5净额, 并按 exit_reason 分桶定位漏血点。

口径 (与既有尺子对齐)
--------------------
* 样本: source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') 白名单 (防跨引擎拼接, 口径教训#1),
  trade_date >= --since (默认 2026-07-11 干净窗口), 已平仓且有 pnl_net。
* 锚日: trade_date (与 exit_scorecard/fee_unit_economics 同一权威列); T+5 = 锚日后第 5 个交易日收盘。
* 价格: data/replay_bars_daily/ 日K缓存 (L2/L3 共享, QFQ); 缺失标的现场补拉, 仍缺失→计入 censored 不静默丢弃。
* 费用: CF 侧保持实际 round-trip 总费不变 (同为两腿; SEC/TAF 尾差二阶略去, 见局限)。
* SHORT 借券敏感性: 名义 × 3%/年 × 5/252 上界估计单列。
* α 变体: 同窗口 SPY 收盘收益扣减 (SPY 缓存独立落 data/t5_cf_spy_bars.json, 不污染共享缓存目录)。

判读边界 (预注册内嵌, 防挪门柱)
------------------------------
本工具是**描述性证据**, 供 PIVOT"更长持有期"赛道判读参考;
8/29 终裁判据仍以 august-verdict-framework 注册的 direction ruler T+3/T+5 门为准,
本工具读数不构成、也不替代任何 GO/PIVOT/STOP 判据。

用法
----
  python scripts/t5_hold_counterfactual.py [--since 2026-07-11] [--no-fetch]

输出
----
  data/t5_hold_cf_latest.json (+ _history.jsonl 追加)
回滚: 删本文件 + 删输出两件 (cron 行如有则删行)。
"""
import argparse
import bisect
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "ftseek_us.db"
DAILY_DIR = PROJECT_ROOT / "data" / "replay_bars_daily"
SPY_CACHE = PROJECT_ROOT / "data" / "t5_cf_spy_bars.json"
OUT_LATEST = PROJECT_ROOT / "data" / "t5_hold_cf_latest.json"
OUT_HISTORY = PROJECT_ROOT / "data" / "t5_hold_cf_history.jsonl"

DEFAULT_SINCE = "2026-07-11"
T_HORIZON = 5          # T+5 个交易日
SHORT_BORROW_ANNUAL = 0.03   # 借券费敏感性上界假设 (SNDR 实测 3.0%/年, §十动态源实测)
BORROW_DAYS = 5 / 252


def _now_et_str() -> str:
    try:
        from app.utils.timezone import now_et
        return now_et().isoformat()
    except Exception:
        return datetime.utcnow().isoformat() + "+00:00(utc-fallback)"


def _cache_path(symbol: str) -> Path:
    return DAILY_DIR / f"{symbol.replace('.', '_')}.json"


def _load_bars(symbol: str, allow_fetch: bool):
    """[[date,o,h,l,c]] 升序; 先缓存后 Futu 补拉 (QFQ, 与 L3 同参)。失败返回 None。"""
    cp = _cache_path(symbol)
    if cp.exists():
        try:
            rows = json.loads(cp.read_text())
            if rows:
                return rows
        except Exception:
            pass
    if not allow_fetch:
        return None
    try:
        from futu import KLType, RET_OK, AuType, OpenQuoteContext
        ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                               port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
        rows: list[list] = []
        page_key = None
        for _page in range(4):  # 窗口仅需数月, 2 页足够, 4 页防御
            ret, data, page_key = ctx.request_history_kline(
                symbol, ktype=KLType.K_DAY, start="2026-01-01", end="2026-12-31",
                max_count=1000, page_req_key=page_key, autype=AuType.QFQ)
            if ret != RET_OK or data is None:
                break
            for _, r in data.iterrows():
                rows.append([str(r.get("time_key", ""))[:10], float(r["open"]), float(r["high"]),
                             float(r["low"]), float(r["close"])])
            if page_key is None:
                break
            time.sleep(1.2)
        ctx.close()
        time.sleep(1.2)
        if not rows:
            return None
        rows.sort(key=lambda b: b[0])
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(rows))
        return rows
    except Exception as e:
        print(f"[t5_cf] {symbol} 补拉失败: {e}")
        return None


def _load_spy(allow_fetch: bool):
    if SPY_CACHE.exists():
        try:
            rows = json.loads(SPY_CACHE.read_text())
            if rows:
                return rows
        except Exception:
            pass
    if not allow_fetch:
        return None
    try:
        from futu import KLType, RET_OK, AuType, OpenQuoteContext
        ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                               port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
        rows: list[list] = []
        page_key = None
        for _page in range(4):
            ret, data, page_key = ctx.request_history_kline(
                "US.SPY", ktype=KLType.K_DAY,
                start="2026-06-01", end="2026-12-31",
                max_count=1000, page_req_key=page_key, autype=AuType.QFQ)
            if ret != RET_OK or data is None:
                break
            for _, r in data.iterrows():
                rows.append([str(r.get("time_key", ""))[:10], float(r["close"])])
            if page_key is None:
                break
            time.sleep(1.2)
        ctx.close()
        time.sleep(1.2)
        if not rows:
            return None
        rows.sort(key=lambda b: b[0])
        SPY_CACHE.write_text(json.dumps(rows))
        return rows
    except Exception as e:
        print(f"[t5_cf] SPY 补拉失败: {e}")
        return None


def load_trades(since: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    rows = conn.execute(
        """
        SELECT id, trade_date, symbol, direction, quantity, entry_price,
               realized_pnl, pnl_net, exit_reason, entry_time, exit_time
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
          AND trade_date >= ?
          AND exit_time IS NOT NULL
          AND pnl_net IS NOT NULL
          AND direction IN ('LONG', 'SHORT')
        ORDER BY id
        """,
        (since,),
    ).fetchall()
    conn.close()
    cols = ["id", "trade_date", "symbol", "direction", "quantity", "entry_price",
            "gross", "net", "exit_reason", "entry_time", "exit_time"]
    return [dict(zip(cols, r)) for r in rows]


def main():
    ap = argparse.ArgumentParser(description="T+5 摆动变体离线反事实 (只读)")
    ap.add_argument("--since", default=DEFAULT_SINCE)
    ap.add_argument("--no-fetch", action="store_true", help="缺缓存不联网补拉, 直接计删失")
    args = ap.parse_args()
    allow_fetch = not args.no_fetch

    trades = load_trades(args.since)
    if not trades:
        print("[t5_cf] 窗口内无样本, 不产出。")
        return

    # ── 按需加载标的日K ──
    syms = sorted({t["symbol"] for t in trades})
    bars_by_sym: dict[str, list] = {}
    dates_idx: dict[str, dict[str, int]] = {}
    missing_syms: list[str] = []
    for s in syms:
        rows = _load_bars(s, allow_fetch)
        if not rows:
            missing_syms.append(s)
            continue
        bars_by_sym[s] = rows
        dates_idx[s] = {b[0]: i for i, b in enumerate(rows)}

    spy_rows = _load_spy(allow_fetch)
    spy_idx = {b[0]: i for i, b in enumerate(spy_rows)} if spy_rows else {}
    spy_fetched = bool(spy_rows)

    censored_missing_bars = 0     # 标的整段无K线
    censored_window_end = 0       # T+5 超出最后bar (窗口右缘删失)
    computed: list[dict] = []

    for t in trades:
        s = t["symbol"]
        if s not in bars_by_sym:
            censored_missing_bars += 1
            continue
        bars = bars_by_sym[s]
        dmap = dates_idx[s]
        # 锚日=trade_date; 若当日非交易日 (节假日入场罕见), 取其后首个交易日
        ai = dmap.get(t["trade_date"])
        if ai is None:
            ai = bisect.bisect_left([b[0] for b in bars], t["trade_date"])
            if ai >= len(bars):
                censored_window_end += 1
                continue
        ti = ai + T_HORIZON
        if ti >= len(bars):
            censored_window_end += 1
            continue
        entry_px = float(t["entry_price"])
        close_t5 = float(bars[ti][4])
        qty = float(t["quantity"])
        sign = 1.0 if t["direction"] == "LONG" else -1.0
        notional = entry_px * qty
        fee_total = float(t["gross"]) - float(t["net"])   # 实际两腿总费, CF 沿用
        gross_actual = float(t["gross"])

        cf_gross = (close_t5 - entry_px) * sign * qty
        cf_net = cf_gross - fee_total
        borrow_adj = 0.0
        if t["direction"] == "SELL_SHORT":
            borrow_adj = notional * SHORT_BORROW_ANNUAL * BORROW_DAYS

        # α 变体: 同窗口 SPY 收盘→收盘
        alpha_usd = None
        si = spy_idx.get(bars[ai][0])
        sj = spy_idx.get(bars[ti][0])
        if si is not None and sj is not None and spy_rows[si][1]:
            spy_ret = spy_rows[sj][1] / spy_rows[si][1] - 1.0
            trade_ret = (cf_gross / notional) if notional else 0.0
            alpha_usd = (trade_ret - spy_ret) * notional

        computed.append({
            "id": t["id"], "symbol": s, "direction": t["direction"],
            "trade_date": t["trade_date"],
            "exit_reason": t["exit_reason"] or "",
            "entry_price": entry_px, "close_t5": close_t5,
            "t5_date": bars[ti][0],
            "notional": round(notional, 2),
            "fee_total": round(fee_total, 2),
            "actual_net": round(float(t["net"]), 2),
            "cf_net_raw": round(cf_net, 2),
            "cf_net_short_borrow_adj": round(cf_net - borrow_adj, 2),
            "alpha_vs_spy_usd": round(alpha_usd, 2) if alpha_usd is not None else None,
            "delta_cf_minus_actual": round(cf_net - float(t["net"]), 2),
            "actual_exit_before_t5": (t["exit_time"] or "")[:10] <= bars[ti][0],
            "_borrow_adj": borrow_adj,
        })

    n = len(computed)
    result = {
        "generated_at": _now_et_str(),
        "tool": "scripts/t5_hold_counterfactual.py",
        "mode": "ro-read-only",
        "window_since": args.since,
        "horizon": f"T+{T_HORIZON} 收盘价",
        "sample": {
            "n_trades_in_window": len(trades),
            "n_computed": n,
            "n_censored_missing_bars": censored_missing_bars,
            "n_censored_window_end": censored_window_end,
            "censored_symbols": missing_syms,
            "spy_alpha_available": spy_fetched,
        },
        "interpretation_boundary": (
            "描述性证据, 仅供 PIVOT'更长持有期'赛道参考; "
            "08-29 终裁判据以 direction_counterfactual_ruler 的预注册 T+3/T+5 门为准, 本读数不替代之。"),
        "verdict_hint_rule": "Δper_trade ≥ +$10 支持摆动 / ±$10 内中性 / ≤ -$10 不支持 (本工具自定描述带)",
    }

    def _agg(key_fn=None, subset=None):
        rows = subset if subset is not None else computed
        if key_fn:
            rows = [r for r in rows if key_fn(r)]
        m = len(rows)
        if m == 0:
            return {"n": 0}
        act = sum(r["actual_net"] for r in rows)
        cf = sum(r["cf_net_short_borrow_adj"] for r in rows)
        wr_act = sum(1 for r in rows if r["actual_net"] > 0) / m * 100
        wr_cf = sum(1 for r in rows if r["cf_net_short_borrow_adj"] > 0) / m * 100
        flip_l2w = sum(1 for r in rows if r["actual_net"] <= 0 < r["cf_net_short_borrow_adj"])
        flip_w2l = sum(1 for r in rows if r["actual_net"] > 0 >= r["cf_net_short_borrow_adj"])
        out = {
            "n": m,
            "n_lt_30_flag": m < 30,
            "actual_net_sum": round(act, 2),
            "cf_net_sum": round(cf, 2),
            "actual_per_trade": round(act / m, 2),
            "cf_per_trade": round(cf / m, 2),
            "delta_per_trade": round((cf - act) / m, 2),
            "wr_actual_pct": round(wr_act, 1),
            "wr_cf_pct": round(wr_cf, 1),
            "flips_lose_to_win": flip_l2w,
            "flips_win_to_lose": flip_w2l,
        }
        alphas = [r["alpha_vs_spy_usd"] for r in rows if r["alpha_vs_spy_usd"] is not None]
        if alphas:
            out["cf_alpha_vs_spy_sum"] = round(sum(alphas), 2)
            out["cf_alpha_vs_spy_per_trade"] = round(sum(alphas) / len(alphas), 2)
        return out

    if n == 0:
        result["overall"] = {"n": 0}
        print("[t5_cf] 全部样本删失, 无可比读数。")
    else:
        # 逐笔明细落档 — DEB 引用可独立复核 (id 对齐 trade_records.id)
        result["trades"] = [
            {k: v for k, v in r.items() if not k.startswith("_")} for r in computed]
        result["overall"] = _agg()
        result["by_direction"] = {
            "LONG": _agg(subset=[r for r in computed if r["direction"] == "LONG"]),
            "SHORT": _agg(subset=[r for r in computed if r["direction"] == "SHORT"]),
        }
        buckets: dict[str, list] = {}
        for r in computed:
            buckets.setdefault(r["exit_reason"] or "(empty)", []).append(r)
        by_reason = {k: _agg(subset=v) for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1]))}
        result["by_exit_reason"] = by_reason
        deltas = sorted(r["delta_cf_minus_actual"] for r in computed)
        result["delta_distribution"] = {
            "p10": round(deltas[int(0.10 * (n - 1))], 2),
            "p50": round(deltas[n // 2], 2),
            "p90": round(deltas[int(0.90 * (n - 1))], 2),
        }

    OUT_LATEST.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with OUT_HISTORY.open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")

    # ── 人读摘要 ──
    print(f"\n=== T+5 反事实 (since {args.since}, 计算 n={n} / 删失 {censored_missing_bars}+{censored_window_end}) ===")
    if n:
        o = result["overall"]
        print(f"实际: 净合计 {o['actual_net_sum']:+} ({o['actual_per_trade']:+}/笔, WR {o['wr_actual_pct']}%)")
        print(f"T+5 : 净合计 {o['cf_net_sum']:+} ({o['cf_per_trade']:+}/笔, WR {o['wr_cf_pct']}%) [含SHORT借券调整]")
        print(f"Δ   : {o['delta_per_trade']:+}/笔  翻转: 亏→赢 {o['flips_lose_to_win']} / 赢→亏 {o['flips_win_to_lose']}")
        if "cf_alpha_vs_spy_per_trade" in o:
            print(f"T+5 α-vs-SPY: {o['cf_alpha_vs_spy_per_trade']:+}/笔")
        print("Top 漏血桶 (by_exit_reason, 按 Δper_trade 升序前5):")
        ranked = [(k, v) for k, v in result.get("by_exit_reason", {}).items() if v.get("n", 0) > 0]
        for k, v in sorted(ranked, key=lambda kv: kv[1].get("delta_per_trade", 0))[:5]:
            print(f"  {k:<22} n={v['n']:<4} Δ {v['delta_per_trade']:+}/笔  (实际 {v['actual_per_trade']:+} → CF {v['cf_per_trade']:+})")
    print(f"落档: {OUT_LATEST.name} (+history)")
    print(result["interpretation_boundary"])


if __name__ == "__main__":
    sys.exit(main())
