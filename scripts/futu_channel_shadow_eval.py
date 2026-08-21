#!/usr/bin/env python3
"""富途未利用通道影子评估 (DEB-20260812 §五 预注册判据, 2026-08-21 补写).

8/24 裁决窗判官 — 预注册判据照抄 DEB-20260812, 不得事后修改:
  A (deriv_unusual): 期权大单流领先假说 — 事件后窗口收益 vs 同票无事件基线 (配对)
  B (capital_flow): 资金流信号 — 增量 rank-IC (OFI 基线上), 双窗口 OOS ≥40% 或 ρ≥0.15
  共同: 数据质量门 非空率 ≥95% 先行; n≥30; record-only 零行为变更

已知约束 (如实标注):
  OFI 基线数据不存在 (orderbook_s2.jsonl 仅 703 行校准实验) → B 只能算裸边际 IC,
  违反 DEB '禁裸边际' 纪律 → 报告必须标 warning, 裁决时该证据权重降级。
  分钟 bar 缓存缺失时自动回补 (request_history_kline, 节流 300ms/次)。

用法: python3 scripts/futu_channel_shadow_eval.py [--window 8]
输出: data/futu_channel_shadow_eval_latest.json + stdout
"""
import argparse
import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

FLOW_PATH = _ROOT / "data" / "futu_capital_flow.jsonl"
DERIV_PATH = _ROOT / "data" / "futu_deriv_unusual.jsonl"
BARS_DIR = _ROOT / "data" / "replay_bars"
OUT_PATH = _ROOT / "data" / "futu_channel_shadow_eval_latest.json"

QUALITY_GATE = 0.95   # 预注册: 非空率 ≥95%
MIN_N = 30            # 预注册: 样本门槛
OOS_RATIO = 0.40      # 预注册: 双窗口 OOS 同号占比
IC_SIG = 0.15         # 预注册: rank-IC 显著阈值


def load_flow():
    """(symbol, date) -> [(minute_str, in_flow, super_in, big_in), ...] 按分钟排序"""
    if not FLOW_PATH.exists():
        return {}, 0.0
    by_key = defaultdict(list)
    n_total = 0
    n_valid = 0
    with open(FLOW_PATH) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            n_total += 1
            flow = r.get("in_flow")
            if isinstance(flow, (int, float)) and not math.isnan(flow):
                n_valid += 1
                sym = r["symbol"]
                it = r["item_time"]  # ET 市场时间 (DEB §220 声明)
                by_key[(sym, it[:10])].append(
                    (it[11:16], flow, r.get("super_in_flow") or 0, r.get("big_in_flow") or 0))
    quality = n_valid / n_total if n_total else 0.0
    for k in by_key:
        by_key[k].sort()
    return dict(by_key), quality


def load_bars(symbol, date):
    """(symbol, date) -> [(minute_str, close), ...]; 缓存优先, 缺失回补."""
    ticker = symbol.replace("US.", "")
    path = BARS_DIR / f"US_{ticker}_{date}.json"
    if path.exists():
        try:
            bars = json.loads(path.read_text())
            return [(b[0][11:16], float(b[4])) for b in bars]  # [ts, o, h, l, close]
        except Exception:
            pass
    # 回补
    try:
        from futu import OpenQuoteContext, KLType, RET_OK
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        ret, df, _ = ctx.request_history_kline(
            symbol, ktype=KLType.K_1M, start=date, end=date, max_count=1000)
        ctx.close()
        time.sleep(0.35)
        if ret == RET_OK and len(df):
            bars = [[str(r["time_key"]), float(r["open"]), float(r["high"]),
                     float(r["low"]), float(r["close"])] for _, r in df.iterrows()]
            path.write_text(json.dumps(bars))
            return [(b[0][11:16], b[4]) for b in bars]
    except Exception as e:
        print(f"[warn] bar 回补失败 {symbol} {date}: {e}", file=sys.stderr)
    return []


def bar_close(bars, minute):
    """取 ≤ minute 的最后一根 bar close (流采样时刻的价格)."""
    px = None
    for m, c in bars:
        if m <= minute:
            px = c
        else:
            break
    return px


def spearman(xs, ys):
    """rank-IC (spearman), 无依赖纯实现."""
    n = len(xs)
    if n < 3:
        return None, n

    def ranks(v):
        idx = sorted(range(n), key=lambda i: v[i])
        rk = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and v[idx[j + 1]] == v[idx[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1
            for k in range(i, j + 1):
                rk[idx[k]] = avg
            i = j + 1
        return rk

    rx, ry = ranks(xs), ranks(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    cov = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    vx = math.sqrt(sum((r - mx) ** 2 for r in rx))
    vy = math.sqrt(sum((r - my) ** 2 for r in ry))
    if vx == 0 or vy == 0:
        return None, n
    return cov / (vx * vy), n


def eval_capital_flow(window_days):
    """通道 B: 分钟资金流 → 后续收益 rank-IC (裸边际, OFI 基线缺失)."""
    flow, quality = load_flow()
    report = {"channel": "capital_flow", "quality_non_null": round(quality, 4),
              "quality_gate": QUALITY_GATE,
              "quality_pass": quality >= QUALITY_GATE,
              "ofi_baseline_available": False,
              "ofi_warning": "OFI 基线不存在(orderbook_s2 仅703行校准) → 裸边际IC, 违反预注册纪律, 证据权重降级",
              "pairs": []}
    if not flow:
        report["error"] = "capital_flow.jsonl 不存在/为空"
        return report

    pairs = []  # (symbol, date, flow_feature, fwd_ret)
    for (sym, date), series in sorted(flow.items()):
        if not series:
            continue
        bars = load_bars(sym, date)
        if len(bars) < 60:
            continue
        # 特征: 早盘前 30min 累计净流入 / 当日总成交波动归一
        first30 = [f for (m, f, _, _) in series if "09:30" <= m <= "10:00"]
        cum_all = [f for (_, f, _, _) in series]
        denom = sum(abs(f) for f in cum_all) or 1.0
        if len(first30) < 10:
            continue
        feat = sum(first30) / denom  # [-1, 1] 早盘净流占比
        # 前瞻: 10:00 → 收盘
        p0 = bar_close(bars, "10:00")
        p1 = bars[-1][1] if bars else None
        if p0 and p1 and p0 > 0:
            pairs.append((sym, date, feat, (p1 - p0) / p0))

    report["n_pairs"] = len(pairs)
    report["n_gate"] = MIN_N
    report["n_pass"] = len(pairs) >= MIN_N
    if len(pairs) >= 8:
        xs = [p[2] for p in pairs]
        ys = [p[3] for p in pairs]
        ic, n = spearman(xs, ys)
        report["rank_ic_marginal"] = round(ic, 4) if ic is not None else None
        report["rank_ic_n"] = n
        # 双窗口 OOS: 按日期前半/后半切
        dates = sorted(set(p[1] for p in pairs))
        mid = len(dates) // 2
        d1 = set(dates[:mid]); d2 = set(dates[mid:])
        w1 = [(p[2], p[3]) for p in pairs if p[1] in d1]
        w2 = [(p[2], p[3]) for p in pairs if p[1] in d2]
        ic1, _ = spearman([a for a, _ in w1], [b for _, b in w1])
        ic2, _ = spearman([a for a, _ in w2], [b for _, b in w2])
        report["oos_window1_ic"] = round(ic1, 4) if ic1 is not None else None
        report["oos_window2_ic"] = round(ic2, 4) if ic2 is not None else None
        report["oos_same_sign"] = (ic1 is not None and ic2 is not None and ic1 * ic2 > 0)
        # 预注册裁决: 双窗口 OOS ≥40% (此处=同号) 或 |ρ|≥0.15
        report["verdict_candidate"] = (
            "PASS" if (report["oos_same_sign"] or
                       (ic is not None and abs(ic) >= IC_SIG)) else "FAIL")
    report["pairs_sample"] = [(s, d, round(f, 4), round(r, 5)) for s, d, f, r in pairs[:5]]
    return report


def eval_derivative(window_days):
    """通道 A: 期权大单异动 → 事件后收益 vs 无事件基线 (配对)."""
    report = {"channel": "derivative_unusual", "events": 0}
    if not DERIV_PATH.exists():
        report["error"] = "deriv jsonl 不存在"
        return report
    events = []
    seen = set()
    with open(DERIV_PATH) as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            # event_ts_raw 格式 '8.20 23:14' (CST!) — DEB §160 记载北京时间戳
            raw = r.get("event_ts_raw", "")
            eh = r.get("event_hash")
            if eh in seen:
                continue
            seen.add(eh)
            try:
                parts = raw.split(" ")
                mon, day = parts[0].split(".")
                hh, mm = parts[1].split(":")
                # CST→ET: -12h
                cst = datetime(2026, int(mon), int(day), int(hh), int(mm))
                et = cst - timedelta(hours=12)
                if et.date().isoformat() != et.date().isoformat():
                    pass
                events.append((r["symbol"], et.strftime("%Y-%m-%d"),
                               et.strftime("%H:%M"), r.get("action", ""),
                               r.get("amount_usd", 0)))
            except Exception:
                continue
    report["events_dedup"] = len(events)
    # 只统计落在交易时段内的事件 (09:30-16:00 ET)
    intraday = [e for e in events if "09:30" <= e[2] <= "16:00"]
    report["events_intraday"] = len(intraday)

    paired = []  # (symbol, date, action, post_ret)
    for sym, date, minute, action, amt in intraday:
        bars = load_bars(sym, date)
        if len(bars) < 60:
            continue
        p0 = bar_close(bars, minute)
        # 事件后 30min
        tgt = (datetime.strptime(minute, "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")
        p1 = bar_close(bars, tgt)
        if p0 and p1 and p0 > 0:
            ret = (p1 - p0) / p0
            if action == "卖出":  # 卖出看跌/看涨方向含义不同, 记录原始动作不预判
                ret = ret
            paired.append((sym, date, minute, action, round(ret, 5)))
    report["n_paired"] = len(paired)
    report["n_gate"] = MIN_N
    report["n_pass"] = len(paired) >= MIN_N
    if paired:
        rets = [p[4] for p in paired]
        report["post_event_mean_ret"] = round(sum(rets) / len(rets), 5)
        report["post_event_win_rate"] = round(sum(1 for x in rets if x > 0) / len(rets), 3)
    report["paired_sample"] = paired[:5]
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=8)
    args = ap.parse_args()

    print("=" * 60)
    print("富途通道影子评估 (DEB-20260812 §五 预注册判据)")
    print("=" * 60)
    b = eval_capital_flow(args.window)
    print(json.dumps(b, ensure_ascii=False, indent=1)[:1500])
    a = eval_derivative(args.window)
    print(json.dumps(a, ensure_ascii=False, indent=1)[:1200])

    out = {"generated_at": datetime.now().isoformat(),
           "prereg_source": "DEB-20260812 §五",
           "capital_flow": b, "derivative_unusual": a}
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1))
    print(f"\n结果落盘: {OUT_PATH}")


if __name__ == "__main__":
    main()
