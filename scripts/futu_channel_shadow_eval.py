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
OFI_PATH = _ROOT / "data" / "futu_ofi_baseline.jsonl"
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


def load_ofi():
    """OFI 基线快照 → (symbol, date) -> [(hhmm, bid1, bid1_qty, ask1, ask1_qty), ...]"""
    out = defaultdict(list)
    if not OFI_PATH.exists():
        return out
    try:
        for line in open(OFI_PATH):
            try:
                r = json.loads(line)
                hhmm = r["ts_et"][11:16]
                out[(r["symbol"], r["ts_et"][:10])].append(
                    (hhmm, float(r["bid1"]), float(r["bid1_qty"]),
                     float(r["ask1"]), float(r["ask1_qty"])))
            except Exception:
                continue
    except Exception:
        pass
    for k in out:
        out[k].sort()
    return out


def ofi_increments(snaps):
    """Cont-Kukanov-Stoikov (2014) 增量 OFI: 相邻快照间的 ΔOFI.

    e_t^B = I(Pb_t>=Pb_{t-1})*Vb_t - I(Pb_t<=Pb_{t-1})*Vb_{t-1}
    e_t^S = I(Pa_t<=Pa_{t-1})*Va_t - I(Pa_t>=Pa_{t-1})*Va_{t-1}
    ΔOFI_t = e_t^B - e_t^S
    """
    incs = []
    for i in range(1, len(snaps)):
        m, pb, vb, pa, va = snaps[i]
        _, pb0, vb0, pa0, va0 = snaps[i - 1]
        eb = ((vb if pb >= pb0 else 0) - (vb0 if pb <= pb0 else 0))
        es = ((va if pa <= pa0 else 0) - (va0 if pa >= pa0 else 0))
        incs.append((m, eb - es))
    return incs


def eval_capital_flow(window_days):
    """通道 B: 分钟资金流 → 后续收益 rank-IC.

    2026-08-21 (Qoder 第5轮 #2/#3/#7 修复):
    - 增量 IC 实现: OFI 基线可用时, 残差化 (rank(flow) 对 rank(ΔOFI) OLS 取残差)
      再对前瞻收益算 rank-IC — 预注册'禁裸边际'的落地. 同时报 flow-ofi 相关
      (弱相关则增量口径退化为裸边际换皮, 8/24 读数必须一并报告).
    - 裁决语义分级: 原'同号即PASS'在零假设下概率~50% → 加 t 检验
      (|t|>=1.96), 分 PASS_SIGNIFICANT / WEAK_EVIDENCE / FAIL 三档.
    - window_days 真消费: 只算最近 N 个交易日.
    """
    flow, quality = load_flow()
    ofi_data = load_ofi()
    ofi_rows = sum(len(v) for v in ofi_data.values())
    ofi_available = ofi_rows >= 100  # 增量口径最少需要连续快照对
    report = {"channel": "capital_flow", "quality_non_null": round(quality, 4),
              "quality_gate": QUALITY_GATE,
              "quality_pass": quality >= QUALITY_GATE,
              "ofi_baseline_available": ofi_available,
              "ofi_baseline_rows": ofi_rows,
              "ofi_warning": (None if ofi_available else
                              "OFI 基线样本不足(<100行) → 裸边际IC, 违反预注册'禁裸边际'纪律, 证据权重降级; "
                              "采集已于 2026-08-21 启动, 攒样中"),
              "pairs": []}
    if not flow:
        report["error"] = "capital_flow.jsonl 不存在/为空"
        return report

    # window_days 真消费: 只保留最近 N 个交易日
    all_dates = sorted(set(d for (_, d) in flow.keys()))
    keep_dates = set(all_dates[-window_days:]) if window_days else set(all_dates)

    pairs = []  # (symbol, date, flow_feature, fwd_ret)
    for (sym, date), series in sorted(flow.items()):
        if date not in keep_dates or not series:
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
        # 显著性 (Qoder #3): t = ic*sqrt(n-2)/sqrt(1-ic^2), 95% 门 |t|>=1.96
        if ic is not None and abs(ic) < 1.0:
            t_stat = ic * math.sqrt(n - 2) / math.sqrt(1 - ic ** 2)
            report["ic_t_stat"] = round(t_stat, 3)
            report["ic_significant_95"] = abs(t_stat) >= 1.96

        # ── 增量 IC (Qoder #2): OFI 残差化 ──
        inc_pairs = []  # (flow_feat, ofi_feat, fwd_ret)
        for sym, date, feat, fwd in pairs:
            snaps = ofi_data.get((sym, date))
            if not snaps or len(snaps) < 3:
                continue
            incs = ofi_increments(snaps)
            early_ofi = sum(d for (m, d) in incs if "09:30" < m <= "10:00")
            scale = sum(abs(d) for (_, d) in incs) or 1.0
            inc_pairs.append((feat, early_ofi / scale, fwd))
        report["ofi_overlap_pairs"] = len(inc_pairs)
        if len(inc_pairs) >= 8:
            fx = [a for a, _, _ in inc_pairs]
            ox = [b for _, b, _ in inc_pairs]
            iy = [c for _, _, c in inc_pairs]
            flow_ofi_corr, _ = spearman(fx, ox)
            report["flow_ofi_rank_corr"] = round(flow_ofi_corr, 4) if flow_ofi_corr is not None else None
            # rank 残差化: rank(flow) 对 rank(ofi) 一元 OLS 取残差
            n2 = len(inc_pairs)
            def _ranks(v):
                idx = sorted(range(n2), key=lambda i: v[i])
                rk = [0.0] * n2
                i = 0
                while i < n2:
                    j = i
                    while j + 1 < n2 and v[idx[j + 1]] == v[idx[i]]:
                        j += 1
                    for k in range(i, j + 1):
                        rk[idx[k]] = (i + j) / 2.0 + 1
                    i = j + 1
                return rk
            rfx, rox = _ranks(fx), _ranks(ox)
            mx, mo = sum(rfx) / n2, sum(rox) / n2
            denom = sum((rox[i] - mo) ** 2 for i in range(n2))
            if denom > 0:
                beta = sum((rfx[i] - mx) * (rox[i] - mo) for i in range(n2)) / denom
                resid = [rfx[i] - mx - beta * (rox[i] - mo) for i in range(n2)]
                ic_inc, _ = spearman(resid, iy)
                report["rank_ic_incremental_vs_ofi"] = round(ic_inc, 4) if ic_inc is not None else None
            else:
                report["rank_ic_incremental_vs_ofi"] = None
                report["incremental_note"] = "OFI 特征零方差, 残差化不可算"
        else:
            report["incremental_note"] = ("OFI 与 capital_flow 同日重叠样本不足 "
                                          f"({len(inc_pairs)}<8) — OFI 采集 8/21 启动, "
                                          "重叠交易日攒样中; 当前仅裸边际 IC")

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
        # 预注册裁决 (Qoder #3 分级): 同号在零假设下概率~50%, 须叠加显著性
        significant = report.get("ic_significant_95", False)
        if significant and report["oos_same_sign"]:
            verdict = "PASS_SIGNIFICANT"
        elif report["oos_same_sign"] or (ic is not None and abs(ic) >= IC_SIG):
            verdict = "WEAK_EVIDENCE"  # 方向性弱证据, 非过门; 8/24 须回预注册原文定语义
        else:
            verdict = "FAIL"
        report["verdict_candidate"] = verdict
    report["pairs_sample"] = [(s, d, round(f, 4), round(r, 5)) for s, d, f, r in pairs[:5]]
    return report


def eval_derivative(window_days):
    """通道 A: 期权大单异动 → 事件后收益 vs 同日无事件基线 (配对).

    2026-08-21 (Qoder 第5轮 #4/#5/#7 修复):
    - 时区: CST→ET 原硬编码 -12h 仅夏令时正确, 冬令时偏1h → 改 zoneinfo.
    - 基线配对: 原实现只有事件后30min均值+胜率, 无法排除日内漂移 →
      每事件对照同日同票 30min 滚动收益中位数基线 (事件时刻除外),
      excess = event_ret - baseline. 基线不足 (<10个网格点) 的事件剔除.
    - 删死代码: L236 恒真if、卖出no-op分支.
    """
    from zoneinfo import ZoneInfo
    _CST = ZoneInfo("Asia/Shanghai")
    _ET = ZoneInfo("America/New_York")
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
            # event_ts_raw 格式 '8.20 23:14' (CST) — DEB §160 记载北京时间戳
            raw = r.get("event_ts_raw", "")
            eh = r.get("event_hash")
            if eh in seen:
                continue
            seen.add(eh)
            try:
                parts = raw.split(" ")
                mon, day = parts[0].split(".")
                hh, mm = parts[1].split(":")
                # CST→ET: zoneinfo (冬令时自动 -13h, 夏令时 -12h)
                cst = datetime(2026, int(mon), int(day), int(hh), int(mm),
                               tzinfo=_CST)
                et = cst.astimezone(_ET)
                events.append((r["symbol"], et.strftime("%Y-%m-%d"),
                               et.strftime("%H:%M"), r.get("action", ""),
                               r.get("amount_usd", 0)))
            except Exception:
                continue
    report["events_dedup"] = len(events)
    # 只统计落在交易时段内的事件 (09:30-16:00 ET)
    intraday = [e for e in events if "09:30" <= e[2] <= "16:00"]
    report["events_intraday"] = len(intraday)

    paired = []  # (symbol, date, action, post_ret, baseline_ret, excess)
    n_no_baseline = 0
    for sym, date, minute, action, amt in intraday:
        bars = load_bars(sym, date)
        if len(bars) < 60:
            continue
        p0 = bar_close(bars, minute)
        # 事件后 30min
        tgt = (datetime.strptime(minute, "%H:%M") + timedelta(minutes=30)).strftime("%H:%M")
        p1 = bar_close(bars, tgt)
        if not (p0 and p1 and p0 > 0):
            continue
        ret = (p1 - p0) / p0
        # 同日无事件基线: 09:30-15:30 每 5min 网格的 30min 收益中位数 (事件点±30min 剔除)
        base_rets = []
        t = 570  # 09:30 in minutes
        ev_min = int(minute[:2]) * 60 + int(minute[3:])
        while t <= 930:  # 至 15:30
            if abs(t - ev_min) >= 30:
                m0 = f"{t//60:02d}:{t%60:02d}"
                m1 = f"{(t+30)//60:02d}:{(t+30)%60:02d}"
                b0 = bar_close(bars, m0)
                b1 = bar_close(bars, m1)
                if b0 and b1 and b0 > 0:
                    base_rets.append((b1 - b0) / b0)
            t += 5
        if len(base_rets) < 10:
            n_no_baseline += 1
            continue
        base_rets.sort()
        baseline = base_rets[len(base_rets)//2]  # 中位数 (抗离群)
        paired.append((sym, date, minute, action, round(ret, 5),
                       round(baseline, 5), round(ret - baseline, 5)))
    report["n_paired"] = len(paired)
    report["n_dropped_no_baseline"] = n_no_baseline
    report["n_gate"] = MIN_N
    report["n_pass"] = len(paired) >= MIN_N
    if paired:
        rets = [p[4] for p in paired]
        excess = [p[6] for p in paired]
        report["post_event_mean_ret"] = round(sum(rets) / len(rets), 5)
        report["post_event_win_rate"] = round(sum(1 for x in rets if x > 0) / len(rets), 3)
        report["baseline_mean_ret"] = round(sum(p[5] for p in paired) / len(paired), 5)
        report["excess_mean_ret"] = round(sum(excess) / len(excess), 5)
        report["excess_win_rate"] = round(sum(1 for x in excess if x > 0) / len(excess), 3)
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
