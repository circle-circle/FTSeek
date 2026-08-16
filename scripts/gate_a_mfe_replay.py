#!/usr/bin/env python3
"""门A 前置·任务二: 规则A 反事实 1-min K 重放 — DEB-20260805 门A 裁决前置.

规则A (预注册, DEB-20260805 §四): 持仓浮盈(美元, 按 bar close 更新) 曾达峰值 ≥ $8 后,
浮盈回撤至峰值的 40% (当前浮盈 ≤ 0.4×峰值浮盈) 即市价离场 (按该 bar close 计).
队列内全部为 SL_BREAKEVEN (trail 未激活), 故规则A 对全部 17 笔生效.

重放窗口: entry→真实 exit_time 的 1-min bars (K线 time_key 为分钟末标签):
  * 首 bar: time_key > floor(entry_time 到分钟)  (与 exit_replay_backtest 同约定;
    对 09:35:54 类入场, 首 bar 09:36 含入场前 ≤1min 价格, 粒度内误差, 如实声明)
  * 末 bar: time_key ≤ exit_time (真实离场时点前已收盘的最后一根 bar)
  * 窗口内未触发 → 反事实 = 实盘同价 (d_i = 0).

配对差 d_i = 规则A毛 − 实盘毛 (费用配对内消去). 检验 (README 门径规范§1 分族②):
  差值符号置换检验 B=10,000 双侧 + 均值/中位 bootstrap CI; 毛/净双列.

诚实性检查:
  * 单峰 vs 多峰: 按 bar close 浮盈序列数"峰段" — 一段在浮盈回落至 ≤0.4×段峰值时
    结束; 段峰值 ≥$8 的段数 ≥2 = 多峰 (规则A 会在第一段回撤离场, 错过后续更高峰).
  * bar 缺失: 逐笔记 bars 期望/实得与缺口分钟数; 整段缺失则剔出检验并如实报告,
    不得编造.
  * close-only 口径: 峰值/触发均按 close, 不用 high/low → 相对"bar 内极值"口径,
    峰值可能低估、触发可能滞后 (双向保守性见报告).

数据源: Futu K_1M, autype=NONE (与 exit_replay_backtest.fetch_bars 同模式:
  磁盘缓存 data/replay_bars/, 空结果重试一次, 300s 限流保护 sleep 1.2s/次).
产出 (幂等覆盖): data/gate_a_replay_2026-08-05/per_trade.csv + replay_summary.json
只读 DB (ro mode); 不改任何交易链路/配置.
用法: venv/bin/python scripts/gate_a_mfe_replay.py [--refresh-bars]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.utils.timezone import ET  # noqa: E402

DB_PATH = REPO / "data" / "ftseek_us.db"
BAR_DIR = REPO / "data" / "replay_bars"
OUT_DIR = REPO / "data" / "gate_a_replay_2026-08-05"

WINDOW_START, WINDOW_END = "2026-07-27", "2026-08-04"
MFE_USD_MIN = 8.0
ACTIVATE_USD = 8.0      # 规则A 激活阈值 (峰值浮盈 ≥ $8)
KEEP_FRAC = 0.4         # 触发线: 当前浮盈 ≤ 0.4×峰值
B_PERM = 10_000
RNG_SEED = 20260805


# ────────────────────────────────────────────────────────────
# 数据装载
# ────────────────────────────────────────────────────────────

def _parse_et(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    assert dt.tzinfo is not None, f"entry/exit_time 应为 aware ET: {ts}"
    return dt.astimezone(ET)


def load_queue(con: sqlite3.Connection) -> list[dict]:
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """
        SELECT id, trade_date, symbol, direction, entry_time, exit_time,
               entry_price, exit_price, quantity, realized_pnl, pnl_net,
               fees_est, mfe_pct, metadata_json
        FROM trade_records
        WHERE exit_reason='SL_BREAKEVEN' AND trade_date BETWEEN ? AND ?
          AND exit_time IS NOT NULL
        ORDER BY id
        """, (WINDOW_START, WINDOW_END)).fetchall()
    out = []
    for r in rows:
        sign = 1.0 if r["direction"].upper() == "LONG" else -1.0
        entry = float(r["entry_price"])
        mfe_usd = float(r["mfe_pct"] or 0.0) / 100.0 * entry * float(r["quantity"])
        if mfe_usd < MFE_USD_MIN:
            continue
        out.append({
            "trade_id": r["id"], "trade_date": r["trade_date"], "symbol": r["symbol"],
            "direction": r["direction"].upper(), "sign": sign,
            "entry_et": _parse_et(r["entry_time"]), "exit_et": _parse_et(r["exit_time"]),
            "entry": entry, "exit": float(r["exit_price"]), "qty": float(r["quantity"]),
            "live_gross": float(r["realized_pnl"] or 0.0),
            "pnl_net": float(r["pnl_net"] or 0.0), "fees_est": float(r["fees_est"] or 0.0),
            "mfe_usd_ledger": mfe_usd,
            "meta": json.loads(r["metadata_json"] or "{}"),
        })
    return out


# ────────────────────────────────────────────────────────────
# 1-min bar 获取 (exit_replay_backtest.fetch_bars 同模式: 缓存/重试/限流)
# ────────────────────────────────────────────────────────────

def _bar_cache_path(symbol: str, day: str) -> Path:
    return BAR_DIR / f"{symbol.replace('.', '_')}_{day}.json"


def _read_cache(symbol: str, day: str) -> list[dict] | None:
    cp = _bar_cache_path(symbol, day)
    if not cp.exists():
        return None
    try:
        raw = json.loads(cp.read_text())
        return [{"t": ET.localize(datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S")),
                 "o": b[1], "h": b[2], "l": b[3], "c": b[4]} for b in raw]
    except Exception:
        return None


def fetch_bars(ctx, symbol: str, day: str, refresh: bool = False) -> list[dict] | None:
    if not refresh:
        cached = _read_cache(symbol, day)
        if cached:
            return cached
    from futu import KLType, RET_OK, AuType
    data = None
    for attempt in (1, 2):  # 空结果重试一次 (区分限流失败 vs 休市合法空)
        ret, data, _ = ctx.request_history_kline(
            symbol, ktype=KLType.K_1M, start=day, end=day, max_count=1000,
            autype=AuType.NONE)  # 原始成交价口径, 禁前复权 (DEB-bar-ruler)
        if ret == RET_OK and data is not None and len(data) > 0:
            break
        if attempt == 1:
            time.sleep(3.0)
    if data is None or len(data) == 0:
        time.sleep(1.2)  # kline 配额保护 (≤25次/30s)
        return None
    bars = [[str(row.get("time_key", ""))[:19], float(row["open"]), float(row["high"]),
             float(row["low"]), float(row["close"])] for _, row in data.iterrows()]
    bars.sort(key=lambda b: b[0])
    cp = _bar_cache_path(symbol, day)
    cp.parent.mkdir(parents=True, exist_ok=True)
    cp.write_text(json.dumps(bars))
    time.sleep(1.2)
    return [{"t": ET.localize(datetime.strptime(b[0], "%Y-%m-%d %H:%M:%S")),
             "o": b[1], "h": b[2], "l": b[3], "c": b[4]} for b in bars]


def collect_bars(queue: list[dict], refresh: bool) -> tuple[dict, list]:
    """返回 (symbol-day→bars, 缺失 symbol-day 列表). 仅在缓存不足时连 OpenD."""
    need = sorted({(tr["symbol"], tr["entry_et"].strftime("%Y-%m-%d")) for tr in queue})
    out, to_fetch = {}, []
    for sym, day in need:
        if not refresh:
            cached = _read_cache(sym, day)
            if cached:
                out[(sym, day)] = cached
                continue
        to_fetch.append((sym, day))
    missing = [k for k in need if k not in out]
    if to_fetch:
        from futu import OpenQuoteContext
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        ctx = OpenQuoteContext(host=host, port=port)
        try:
            for sym, day in to_fetch:
                bars = fetch_bars(ctx, sym, day, refresh=True)
                if bars:
                    out[(sym, day)] = bars
                    if (sym, day) in missing:
                        missing.remove((sym, day))
                print(f"  fetch {sym} {day}: {'OK' if bars else 'EMPTY'}")
        finally:
            ctx.close()
    print(f"bars: {len(out)}/{len(need)} symbol-days 就绪"
          + (f", 缺失: {missing}" if missing else ""))
    return out, missing


# ────────────────────────────────────────────────────────────
# 规则A 重放
# ────────────────────────────────────────────────────────────

def replay_rule_a(tr: dict, bars: list[dict]) -> dict:
    """在 entry→exit 窗口的 bar close 序列上重放规则A. 返回逐笔结果 dict."""
    entry_et, exit_et, entry, qty, sign = (tr["entry_et"], tr["exit_et"], tr["entry"],
                                           tr["qty"], tr["sign"])
    lo = entry_et.replace(second=0, microsecond=0)
    win = [b for b in bars if lo < b["t"] <= exit_et]

    def profit(px: float) -> float:
        return (px - entry) * qty * sign

    # 期望 bar 数 (RTH 分钟末标签 09:31~16:00) 与缺口 — 从入场分钟+1 起算,
    # 开头缺 bar 也计入缺口
    expected = 0
    t = lo + timedelta(minutes=1)
    eod = exit_et.replace(hour=16, minute=0, second=0, microsecond=0)
    while t <= min(exit_et, eod):
        if t.hour > 9 or t.minute > 30:  # ≥09:31 标签
            expected += 1
        t += timedelta(minutes=1)
    gaps = expected - len(win)

    # 全窗口 close 浮盈序列 (峰段/多峰分析看完整路径, 不截断在触发点)
    profits = [profit(b["c"]) for b in win]

    # ── 规则A 触发扫描 ──
    peak = 0.0
    triggered = False
    trig_bar = None
    trig_idx = -1
    peak_at_trigger = 0.0
    for i, b in enumerate(win):
        p = profits[i]
        peak = max(peak, p)
        if peak >= ACTIVATE_USD and p <= KEEP_FRAC * peak:
            triggered, trig_bar, trig_idx = True, b, i
            peak_at_trigger = peak
            break

    # ── 峰段统计 (单峰/多峰诚实性检查, 全窗口) ──
    episodes: list[float] = []
    cur: float | None = None
    for p in profits:
        if cur is None:
            if p > 0:
                cur = p
        elif p > cur:
            cur = p
        elif p <= KEEP_FRAC * cur:
            episodes.append(cur)
            cur = None
    if cur is not None:
        episodes.append(cur)
    peaks_ge8 = [e for e in episodes if e >= ACTIVATE_USD]
    global_peak = max(profits) if profits else 0.0
    global_peak_t = win[profits.index(global_peak)]["t"] if profits else None
    # 触发后是否出现比触发时峰值更高的 close 峰 (多峰代价)
    post_max = max(profits[trig_idx + 1:]) if triggered and trig_idx + 1 < len(profits) else None
    missed_higher = bool(triggered and post_max is not None
                         and post_max > peak_at_trigger + 1e-9)

    if triggered:
        cf_price, cf_time = trig_bar["c"], trig_bar["t"]
        cf_reason = "RULE_A_TRIGGER"
        cf_gross = (cf_price - entry) * qty * sign
    else:
        cf_price, cf_time = tr["exit"], exit_et
        cf_reason = "NO_TRIGGER=实盘同价"
        cf_gross = tr["live_gross"]  # 同价 → 同毛, 按构造 d=0 (避免存储舍入噪声)

    return {
        **{k: tr[k] for k in ("trade_id", "trade_date", "symbol", "direction",
                              "live_gross", "pnl_net", "fees_est", "mfe_usd_ledger")},
        "entry": entry, "qty": qty, "exit_et": exit_et.isoformat(),
        "bars_in_window": len(win), "bars_expected": expected, "bar_gap_min": gaps,
        "peak_usd_close": round(peak_at_trigger if triggered else peak, 2),
        "window_peak_usd_close": round(peak, 2),
        "replay_peak_vs_ledger": round(peak - tr["mfe_usd_ledger"], 2),
        "triggered": triggered, "cf_reason": cf_reason,
        "cf_price": round(cf_price, 6), "cf_time": cf_time.isoformat(),
        "cf_gross": round(cf_gross, 4),
        "d_gross": round(cf_gross - tr["live_gross"], 4),
        "peak_episodes_ge8": len(peaks_ge8),
        "multi_peak": len(peaks_ge8) >= 2,
        "episodes_detail": [round(e, 2) for e in episodes],
        "global_peak_usd": round(global_peak, 2),
        "global_peak_time": global_peak_t.isoformat() if global_peak_t else None,
        "trigger_missed_higher_peak": missed_higher,
    }


# ────────────────────────────────────────────────────────────
# 统计检验 (README 门径规范§1 分族②: 配对差符号置换 + bootstrap CI)
# ────────────────────────────────────────────────────────────

def paired_tests(d: np.ndarray) -> dict:
    n = len(d)
    rng = np.random.default_rng(RNG_SEED)
    s_obs = float(d.sum())
    signs = rng.choice(np.array([-1.0, 1.0]), size=(B_PERM, n))
    s_perm = signs @ d
    p_two = float((np.abs(s_perm) >= abs(s_obs)).sum() + 1) / (B_PERM + 1)
    idx = rng.integers(0, n, size=(B_PERM, n))
    boot = d[idx]
    mean_ci = np.percentile(boot.mean(axis=1), [2.5, 97.5])
    med_ci = np.percentile(np.median(boot, axis=1), [2.5, 97.5])
    return {
        "n": n, "sum_d": round(s_obs, 2),
        "mean_d": round(float(d.mean()), 3), "sd_d": round(float(d.std(ddof=1)), 3),
        "median_d": round(float(np.median(d)), 3),
        "n_pos": int((d > 0).sum()), "n_zero": int((d == 0).sum()),
        "n_neg": int((d < 0).sum()),
        "sign_perm_B": B_PERM, "p_two_sided": round(p_two, 5),
        "mean_ci95": [round(float(mean_ci[0]), 3), round(float(mean_ci[1]), 3)],
        "median_ci95": [round(float(med_ci[0]), 3), round(float(med_ci[1]), 3)],
    }


# ────────────────────────────────────────────────────────────
# 主流程
# ────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh-bars", action="store_true", help="强制重新抓取全部 bars")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    queue = load_queue(con)
    con.close()
    print(f"队列: n={len(queue)} (SL_BREAKEVEN, MFE≥${MFE_USD_MIN:.0f}, "
          f"{WINDOW_START}~{WINDOW_END})")

    bars_map, missing_days = collect_bars(queue, args.refresh_bars)

    results, excluded = [], []
    for tr in queue:
        day = tr["entry_et"].strftime("%Y-%m-%d")
        bars = bars_map.get((tr["symbol"], day))
        if not bars:
            excluded.append({"trade_id": tr["trade_id"], "symbol": tr["symbol"],
                             "reason": f"bars 缺失 ({tr['symbol']} {day}) — 不得编造, 剔出检验"})
            continue
        results.append(replay_rule_a(tr, bars))

    # 整段缺口笔 (窗口内缺 bar >0) 保留在检验内但显式标注
    gap_rows = [r for r in results if r["bar_gap_min"] > 0]

    d = np.array([r["d_gross"] for r in results], dtype=float)
    fees = np.array([r["fees_est"] for r in results], dtype=float)
    test = paired_tests(d)
    sum_live = float(np.sum([r["live_gross"] for r in results]))
    sum_cf = float(np.sum([r["cf_gross"] for r in results]))
    sum_fees = float(fees.sum())
    sum_live_net = float(np.sum([r["live_gross"] - r["fees_est"] for r in results]))
    sum_cf_net = float(np.sum([r["cf_gross"] - r["fees_est"] for r in results]))
    n_trig = sum(1 for r in results if r["triggered"])
    n_multi = sum(1 for r in results if r["multi_peak"])
    n_missed = sum(1 for r in results if r["trigger_missed_higher_peak"])

    # 选样恒等式核查: 若 d 几乎全 ≥0 且零来自"未触发", 说明算术恒等式仍然主导
    identity_note = (
        f"d_i≥0 共 {int((d >= 0).sum())}/{len(d)} (其中 0 值 {int((d == 0).sum())} 笔=未触发"
        f"按实盘同价); 队列构造 (SL_BREAKEVEN≈保本落袋 + MFE≥$8) 下首阶近似必有 d>0 — "
        f"符号置换 p 含选样恒等式成分, 真正增量证据须看触发笔的重放价本身 (见报告诚实性声明)."
        if int((d >= 0).sum()) == len(d) else
        f"d_i 出现 {int((d < 0).sum())} 笔负值 — 重放价已脱离选样恒等式域.")

    print("\n=== 逐笔重放 (rule A: 峰值≥$8 后回撤至 0.4×峰值, bar close) ===")
    for r in results:
        print(f"trade {r['trade_id']:>4} {r['trade_date']} {r['symbol']:<9} "
              f"{r['direction']:<5} bars={r['bars_in_window']:>3}/{r['bars_expected']:>3} "
              f"peak${r['peak_usd_close']:>7.2f}(台账{r['mfe_usd_ledger']:>6.2f}) "
              f"{'触发@' + r['cf_time'][11:16] if r['triggered'] else '未触发':<14} "
              f"cf毛={r['cf_gross']:>7.2f} 实盘毛={r['live_gross']:>6.2f} "
              f"d={r['d_gross']:>+7.2f} 峰段≥8: {r['peak_episodes_ge8']}"
              f"{' 多峰' if r['multi_peak'] else ''}"
              f"{' 错过更高峰' if r['trigger_missed_higher_peak'] else ''}")

    print("\n=== 汇总 ===")
    print(f"重放 n={len(results)} (剔出 {len(excluded)}: {excluded or '无'}; "
          f"窗口内 bar 缺口笔: {[ (r['trade_id'], r['symbol'], r['bar_gap_min']) for r in gap_rows] or '无'})")
    print(f"触发 {n_trig}/{len(results)} 笔; 单峰 {len(results) - n_multi} / 多峰 {n_multi} "
          f"(触发且错过后续更高峰 {n_missed})")
    print(f"Σ实盘毛=${sum_live:.2f} → Σ规则A毛=${sum_cf:.2f} (Δ={sum_cf - sum_live:+.2f})")
    print(f"Σ实盘净=${sum_live_net:.2f} → Σ规则A净=${sum_cf_net:.2f} "
          f"(费用 Σ${sum_fees:.2f} 配对内消去, Δ不变)")
    print(f"配对差 d: 均值 {test['mean_d']:+.3f} [CI95 {test['mean_ci95']}], "
          f"中位 {test['median_d']:+.3f} [CI95 {test['median_ci95']}], SD {test['sd_d']}, "
          f"+/0/- = {test['n_pos']}/{test['n_zero']}/{test['n_neg']}")
    print(f"符号置换 B={B_PERM} 双侧 p = {test['p_two_sided']}")
    print(f"诚实性: {identity_note}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cols = ["trade_id", "trade_date", "symbol", "direction", "bars_in_window",
            "bars_expected", "bar_gap_min", "peak_usd_close", "mfe_usd_ledger",
            "replay_peak_vs_ledger", "triggered", "cf_reason", "cf_price", "cf_time",
            "cf_gross", "live_gross", "d_gross", "fees_est", "peak_episodes_ge8",
            "multi_peak", "episodes_detail", "global_peak_usd", "global_peak_time",
            "trigger_missed_higher_peak"]
    with open(OUT_DIR / "per_trade.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in results:
            row = dict(r)
            row["episodes_detail"] = json.dumps(r["episodes_detail"])
            w.writerow(row)
    summary = {
        "generated_at": datetime.now(ET).isoformat(timespec="seconds"),
        "queue": {"window": [WINDOW_START, WINDOW_END], "n": len(queue),
                  "filter": "SL_BREAKEVEN AND MFE_usd>=8"},
        "rule_a": {"activate_usd": ACTIVATE_USD, "keep_frac": KEEP_FRAC,
                   "price_basis": "1-min bar close", "autype": "NONE",
                   "window": "entry_minute_floor < time_key <= exit_time",
                   "no_trigger_fallback": "实盘同价"},
        "replayed": len(results), "excluded": excluded,
        "bar_gap_trades": [{"trade_id": r["trade_id"], "symbol": r["symbol"],
                            "gap_min": r["bar_gap_min"]} for r in gap_rows],
        "n_triggered": n_trig,
        "path_shape": {"single_peak": len(results) - n_multi, "multi_peak": n_multi,
                       "triggered_but_missed_higher_peak": n_missed,
                       "multi_peak_ids": [r["trade_id"] for r in results if r["multi_peak"]]},
        "gross": {"live": round(sum_live, 2), "rule_a": round(sum_cf, 2),
                  "delta": round(sum_cf - sum_live, 2)},
        "net": {"live": round(sum_live_net, 2), "rule_a": round(sum_cf_net, 2),
                "delta": round(sum_cf_net - sum_live_net, 2),
                "fees_sum": round(sum_fees, 2),
                "note": "费用按笔配对消去, 净Δ=毛Δ"},
        "paired_test": test,
        "selection_identity_note": identity_note,
        "per_trade": results,
    }
    (OUT_DIR / "replay_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"\n产出: {OUT_DIR / 'per_trade.csv'}\n      {OUT_DIR / 'replay_summary.json'}")


if __name__ == "__main__":
    main()
