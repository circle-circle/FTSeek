#!/usr/bin/env python3
"""价位自适应影子重放 (2026-08-14) — 纯离线分析, 不碰任何实盘逻辑

目的: 用真实1分钟K线历史路径, 重放 8/4 以来全部成交, 对照:
  实际结果 vs V1(ATR自适应TP/SL) vs V2(60分钟死胎早退)
方法: bar触及近似 (粗筛级, 非轮询级; 结论标"方向可信/幅度待验")

规则对齐现行实盘 (unified_config + exit_shadow baseline_be0.5_dd0.8):
  - 保本: 浮盈≥0.5% 后, 止损上移至入场价(+费垫0.15%)
  - 回撤锁利: 保本激活后, 从峰值回撤0.8%平仓
  - TP/SL 触及即平; 15:52 ET 前未触发按最后一根bar收盘平(PRE_CLOSE)

变体:
  V1: TP=入场±1.5×ATR%, SL=入场∓1.0×ATR% (替代固定~7.5%/~5%模板)
  V2: 实际规则 + 持仓60min时若 MFE<0.1% 且浮亏≤-0.3% → 立即离场

用法: /opt/ftseek/FTSeek-US/venv/bin/python3.11 exit_replay_shadow.py
"""
import json
import sqlite3
import sys
from datetime import datetime

from futu import OpenQuoteContext, KLType, RET_OK

DB = "/opt/ftseek/FTSeek-US/data/ftseek_us.db"
BE_PCT = 0.5          # 保本激活阈值 %
DD_PCT = 0.8          # 峰值回撤锁利 %
FEE_CUSHION = 0.15    # 保本位费垫 %
PRE_CLOSE_MIN = "15:52"

_bar_cache = {}


def get_bars(ctx, symbol, date):
    key = (symbol, date)
    if key in _bar_cache:
        return _bar_cache[key]
    ret, data, _ = ctx.request_history_kline(
        symbol, ktype=KLType.K_1M, start=date, end=date, max_count=1000)
    bars = []
    if ret == RET_OK and data is not None and len(data):
        for _, row in data.iterrows():
            bars.append({
                "t": row["time_key"][11:16],
                "o": float(row["open"]), "h": float(row["high"]),
                "l": float(row["low"]), "c": float(row["close"]),
            })
    _bar_cache[key] = bars
    return bars


def replay(bars, entry_time, entry_price, direction, sl, tp,
           abort_60min=False, qty=1, fee=0.0):
    """沿1min路径重放规则, 返回 (exit_price, exit_time, exit_tag)"""
    sign = 1.0 if direction == "LONG" else -1.0
    entry_t = entry_time[11:16]
    path = [b for b in bars if b["t"] >= entry_t]
    if not path:
        return None, None, "NO_PATH"

    peak = entry_price
    be_active = False
    stop = sl

    for b in path:
        hi, lo = b["h"], b["l"]
        # 更新峰值 (仅在创新高/新低时更新)
        cur_extreme = hi if sign > 0 else lo
        if sign * (cur_extreme - peak) > 0:
            peak = cur_extreme
        peak_gain = sign * (peak - entry_price) / entry_price * 100

        # 保本激活
        if not be_active and peak_gain >= BE_PCT:
            be_active = True
            be_stop = entry_price * (1 + sign * FEE_CUSHION / 100)
            stop = be_stop if sign * (be_stop - stop) > 0 else stop

        # 回撤锁利止损位 (保本激活后)
        if be_active:
            dd_stop = peak * (1 - sign * DD_PCT / 100)
            eff_stop = dd_stop if sign * (dd_stop - stop) > 0 else stop
        else:
            eff_stop = stop

        # V2: 60分钟死胎早退
        if abort_60min and b["t"] >= _add_min(entry_t, 60):
            mfe = peak_gain
            cur_pnl = sign * (b["o"] - entry_price) / entry_price * 100
            if mfe < 0.1 and cur_pnl <= -0.3:
                return b["o"], b["t"], "ABORT_60M"

        # 止损/保本/锁利 触及 (bar内先检查不利方向 — 保守口径, 2026-08-14 Qoder 修正顺序)
        if sign > 0 and lo <= eff_stop:
            return eff_stop, b["t"], "STOP"
        if sign < 0 and hi >= eff_stop:
            return eff_stop, b["t"], "STOP"
        # TP 触及
        if sign > 0 and hi >= tp:
            return tp, b["t"], "TP"
        if sign < 0 and lo <= tp:
            return tp, b["t"], "TP"

        # 收盘前平仓
        if b["t"] >= PRE_CLOSE_MIN:
            return b["c"], b["t"], "PRE_CLOSE"

    return path[-1]["c"], path[-1]["t"], "EOD"


def _add_min(hhmm, delta):
    h, m = int(hhmm[:2]), int(hhmm[3:])
    total = h * 60 + m + delta
    return f"{total // 60:02d}:{total % 60:02d}"


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    trades = [dict(r) for r in con.execute("""
        SELECT symbol, trade_date, entry_time, entry_price, direction, quantity,
               pnl_net, fees_est, exit_reason, factor_detail, metadata_json
        FROM trade_records WHERE entry_time >= '2026-08-04'
        ORDER BY entry_time""")]
    print(f"样本: {len(trades)}笔", file=sys.stderr)

    ctx = OpenQuoteContext(host='127.0.0.1', port=11111)
    results = []
    for t in trades:
        try:
            fd = json.loads(t["factor_detail"] or "{}")
            md = json.loads(t["metadata_json"] or "{}")
            atr_pct = (fd.get("ts_ctx") or {}).get("atr_percent")
            sl_real, tp_real = md.get("stop_loss"), md.get("take_profit")
            if not atr_pct or not sl_real or not tp_real:
                continue
            bars = get_bars(ctx, t["symbol"], t["trade_date"])
            if not bars:
                continue
            ep = t["entry_price"]
            sign = 1 if t["direction"] == "LONG" else -1
            qty = t["quantity"] or 1
            fee = t["fees_est"] or 0

            # 实际规则重放 (sanity)
            px, tm, tag = replay(bars, t["entry_time"], ep, t["direction"],
                                 sl_real, tp_real)
            # V1: ATR自适应 TP=1.5ATR, SL=1.0ATR
            v1_tp = ep * (1 + sign * 1.5 * atr_pct / 100)
            v1_sl = ep * (1 - sign * 1.0 * atr_pct / 100)
            v1 = replay(bars, t["entry_time"], ep, t["direction"], v1_sl, v1_tp)
            # V2: 实际价位 + 60min死胎早退
            v2 = replay(bars, t["entry_time"], ep, t["direction"],
                        sl_real, tp_real, abort_60min=True)

            def pnl_of(r):
                if not r or r[0] is None:
                    return None
                return sign * (r[0] - ep) * qty - fee

            results.append({
                "symbol": t["symbol"], "date": t["trade_date"],
                "time": t["entry_time"][11:16], "dir": t["direction"],
                "atr": atr_pct,
                "actual_pnl": t["pnl_net"], "actual_exit": t["exit_reason"],
                "sim_actual": pnl_of((px, tm, tag)), "sim_tag": tag,
                "v1_pnl": pnl_of(v1), "v1_tag": v1[2] if v1 else None,
                "v1_tp_atr": round(abs(v1_tp / ep - 1) * 100, 2),
                "v2_pnl": pnl_of(v2), "v2_tag": v2[2] if v2 else None,
            })
        except Exception as e:
            print(f"ERR {t['symbol']}: {e}", file=sys.stderr)
    ctx.close()

    out = "/opt/ftseek/FTSeek-US/data/analysis/exit_replay_20260814.json"
    with open(out, "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

    # 汇总 (2026-08-14 Qoder 口径修正: 建模缺口与变体效果分离, 旧版"Δ vs 实际"
    #   把缺口记在变体头上 — 实测缺口 +267 而 V1 机内效果=0, 直接读旧输出会得出反转结论)
    def s(k):
        vs = [r[k] for r in results if r[k] is not None]
        return round(sum(vs), 1), len(vs)

    a_n, _ = s("actual_pnl")
    sa_n, _ = s("sim_actual")
    v1_n, v1_c = s("v1_pnl")
    v2_n, v2_c = s("v2_pnl")
    print(f"\n=== 重放汇总 (n={len(results)}, bar触及近似=粗筛级) ===")
    print(f"实际净盈亏 (DB原值):        {a_n}")
    print(f"模拟实际规则重放 (sanity):  {sa_n} | 建模缺口 = {round(sa_n - a_n, 1)}"
          f"  ← 重放仅建模 BE/DD/TP/SL/PRE_CLOSE, 缺 MFE_STALL/DEAD_SIGNAL/TIME_EXIT 路径,"
          f" 缺口非任何变体效果, 缺口大则 vs实际 的对照无效")
    print(f"V1 ATR自适应TP/SL: {v1_n} (n={v1_c}) | 变体效果 Δ vs 模拟实际 = {round(v1_n - sa_n, 1)}"
          f"  (0 = 初始SL/TP在本样本从不绑定, 退出全由保本/回撤/PRE_CLOSE腿决定)")
    print(f"V2 +60min死胎早退: {v2_n} (n={v2_c}) | 变体效果 Δ vs 模拟实际 = {round(v2_n - sa_n, 1)}")
    print(f"结论门槛: 仅 |建模缺口| 小 (量级≤总pnl 10%) 时才可读 vs实际 对照; 否则只读机内 Δ")

    # V1 明细: TP到不了的比例变化
    print(f"\n落盘: {out}")


if __name__ == "__main__":
    main()
