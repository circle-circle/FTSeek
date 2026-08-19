#!/usr/bin/env python3
"""入场带 A/B 回放: 扁平 band vs 波动率化 band (2026-08-19, 用户批准).

目的: entry_band.py 注释中"band 波动率化"待评估项的量化裁决依据.
扁平带 (现状, 线上常量): knife=3% / chase=5% 一刀切.
波动率化候选: knife = clamp(0.9*ATR%, 2, 6), chase = clamp(1.5*ATR%, 3, 9)
  — 中位 ATR (3.36%) 处与扁平带完全重合 (零行为变更基准),
    低波票收窄 (防噪音带过宽乱触发), 高波票放宽 (防正常波动被挡出带).

触发判定逐字复刻 app/playbook/pipeline.py L4396-4419 (_check_intraday_trigger):
  SHORT: cur>=entry → pullback, triggered = cur <= entry*(1+knife)
         entry*(1-chase) <= cur < entry → chase, triggered = vol_ok
         否则 out_of_band
  BUY:   cur<=entry → pullback, triggered = cur >= entry*(1-knife)
         entry < cur <= entry*(1+chase) → chase, triggered = vol_ok
         否则 out_of_band
触发窗: ET [09:35, 14:00) — 与线上 config 实测一致
  (entry_open_blackout_enabled=True hhmm=0935; cutoff 默认 1400).
量能门: today_cum_vol / (avg5d_daily_vol * elapsed_fraction) >= 1.5,
  开盘15min豁免窗 fail-open (pipeline.py L4360-4381 同款).
  avg5d_daily_vol 用 1分K 前5交易日全天累计量均值 (日K无volume列).

路径质量 (触发 bar 收盘价=入场基准, 当日剩余 1分K, 不隔夜):
  MFE% / MAE% (方向感知), 路径PnL = EOD收盘 vs 入场基准 (方向感知),
  SL触及 = 剩余 bar low/high 是否穿越 instruction.stop_loss.

用法: PYTHONPATH=. venv/bin/python scripts/entry_band_replay.py
落盘: data/entry_band_replay_latest.json
"""
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
BARS_1M = DATA / "replay_bars_1m"
DB = DATA / "ftseek_us.db"

# ── 策略参数 ──
FLAT = {"knife": 3.0, "chase": 5.0}  # entry_band.py 线上常量 (KNIFE_FLOOR_PCT/CHASE_ZONE_PCT)


def vol_band(atr_pct):
    """波动率化候选带: 中位ATR处与扁平带重合."""
    a = atr_pct * 100.0
    knife = max(2.0, min(6.0, 0.9 * a))
    chase = max(3.0, min(9.0, 1.5 * a))
    return {"knife": knife, "chase": chase}


def vol_widen_band(atr_pct):
    """对照臂: 只放宽不收窄 — 低波票保持扁平下限 (首轮实证收窄有害)."""
    a = atr_pct * 100.0
    knife = max(3.0, min(6.0, 0.9 * a))
    chase = max(5.0, min(9.0, 1.5 * a))
    return {"knife": knife, "chase": chase}


def load_bars_1m(sym_db):
    """sym_db: 'US.NVDA' → 加载 US_NVDA.json, 返回 [(ts,o,h,l,c,v),...]"""
    p = BARS_1M / (sym_db.replace(".", "_") + ".json")
    if not p.exists():
        return None
    try:
        with open(p, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return None
    out = []
    for b in raw:
        try:
            out.append((b[0], float(b[1]), float(b[2]), float(b[3]), float(b[4]), float(b[5])))
        except Exception:
            continue
    return out


def group_by_date(bars):
    days = {}
    for b in bars:
        d = b[0][:10]
        days.setdefault(d, []).append(b)
    return days


def elapsed_fraction(bars_so_far):
    """已过交易时长占比 (390 min 常规日)."""
    return max(len(bars_so_far), 1) / 390.0


def simulate_instruction(inst, bars_by_date, sorted_dates, vol_gate=True,
                         online_gates=False):
    """对单条指令在两种 band 下模拟触发. 返回 dict 或 None (无当日数据).

    online_gates=True (§二十九 Qoder F2/F3/F4, 真基线):
      - 量能门阈值 0.8 (unified_config.yaml:1065, 非代码默认1.5)
      - 低ATR门: atr_pct<3% 不触发 (low_atr_gate_min_atr_pct=0.03, RFC-060 d7f51c4d)
      - extreme_gap: |cur-entry|/entry>8% 不触发 (pipeline L4466-4468)
    """
    sym, date, direction, entry, atr_pct, sl = (
        inst["symbol"], inst["date"][:10], inst["direction"],
        float(inst["entry_price"]), inst["atr_pct"], inst["stop_loss"])
    is_short = direction == "SELL_SHORT"
    day_bars = bars_by_date.get(date)
    if not day_bars:
        return None
    idx = sorted_dates.index(date) if date in sorted_dates else -1
    if idx < 5:
        return None  # 前5日均量算不出 → 量能门无基准, 跳过
    # 低ATR门 (线上 3%, RFC-060): 整个指令不可触发
    if online_gates and atr_pct is not None and 0 < atr_pct < 0.03:
        return {"symbol": sym, "date": date, "direction": direction,
                "entry": entry, "atr_pct": atr_pct, "stop_loss": sl,
                "online_gate_skip": "low_atr<3%"}
    # 前5交易日全天累计量 (1分K自聚合)
    prev_vols = []
    for pd in sorted_dates[idx - 5:idx]:
        prev_vols.append(sum(b[5] for b in bars_by_date[pd]))
    avg5d = sum(prev_vols) / len(prev_vols) if prev_vols else 0.0

    band_vol = vol_band(atr_pct) if (atr_pct and atr_pct > 0) else dict(FLAT)
    band_widen = vol_widen_band(atr_pct) if (atr_pct and atr_pct > 0) else dict(FLAT)
    strategies = {"flat": FLAT, "vol": band_vol, "vol_widen": band_widen}
    out = {"symbol": sym, "date": date, "direction": direction,
           "entry": entry, "atr_pct": atr_pct, "stop_loss": sl}
    for name, bd in strategies.items():
        knife, chase = bd["knife"] / 100.0, bd["chase"] / 100.0
        trig = None  # (bar_idx, path, trigger_price)
        cum_vol = 0.0
        _vol_th = 0.8 if online_gates else 1.5  # F2: 线上 unified_config.yaml:1065
        for i, b in enumerate(day_bars):
            hhmm = int(b[0][11:13]) * 100 + int(b[0][14:16])
            cum_vol += b[5]
            if hhmm < 935 or hhmm >= 1400:
                continue  # 触发窗 [09:35, 14:00)
            cur = b[4]  # bar 收盘价 = 该轮实时价
            # F4: extreme_gap 过滤 |cur-entry|/entry > 8% (pipeline L4466-4468)
            if online_gates and entry > 0 and abs(cur - entry) / entry > 0.08:
                continue
            # 量能门 (pipeline L4371-4381 复刻): 开盘15min豁免; 数据缺失fail-open
            mins_since_open = (int(b[0][11:13]) * 60 + int(b[0][14:16])) - (9 * 60 + 30)
            in_grace = 0 <= mins_since_open < 15
            if vol_gate and not in_grace and avg5d > 0:
                vr = cum_vol / (avg5d * elapsed_fraction(day_bars[:i + 1]))
                vol_ok = vr >= _vol_th
            else:
                vol_ok = True
            if is_short:
                if cur >= entry:
                    hit, path = cur <= entry * (1 + knife), "pullback"
                elif cur >= entry * (1 - chase):
                    hit, path = vol_ok, "chase"
                else:
                    hit, path = False, "out_of_band"
            else:
                if cur <= entry:
                    hit, path = cur >= entry * (1 - knife), "pullback"
                elif cur <= entry * (1 + chase):
                    hit, path = vol_ok, "chase"
                else:
                    hit, path = False, "out_of_band"
            if hit:
                trig = (i, path, cur)
                break
        rec = {"triggered": trig is not None, "path": None, "trigger_price": None,
               "band_knife_pct": bd["knife"], "band_chase_pct": bd["chase"]}
        if trig:
            ti, path, tprice = trig
            rec["path"] = path
            rec["trigger_price"] = round(tprice, 4)
            rec["trigger_hhmm"] = day_bars[ti][0][11:16]
            # 路径质量: 触发 bar 之后当日剩余 (含触发 bar)
            rest = day_bars[ti:]
            sgn = -1.0 if is_short else 1.0
            if is_short:
                mfe = (tprice - min(b[3] for b in rest)) / tprice * 100.0
                mae = (max(b[2] for b in rest) - tprice) / tprice * 100.0
                sl_touch = any(b[2] >= sl for b in rest) if sl and sl > 0 else None
            else:
                mfe = (max(b[2] for b in rest) - tprice) / tprice * 100.0
                mae = (tprice - min(b[3] for b in rest)) / tprice * 100.0
                sl_touch = any(b[3] <= sl for b in rest) if sl and sl > 0 else None
            eod = rest[-1][4]
            rec["mfe_pct"] = round(sgn * mfe if not is_short else mfe, 3)
            rec["mae_pct"] = round(mae, 3)
            rec["path_pnl_pct"] = round((eod - tprice) / tprice * 100.0 * sgn, 3)
            rec["sl_touch"] = sl_touch
        out[name] = rec
    return out


def pct(x):
    return round(x * 100, 1)


def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    syms = sorted({f.replace(".json", "") for f in os.listdir(BARS_1M)})
    db_syms = [s.replace("_", ".", 1) for s in syms]
    q = ('SELECT symbol, date, direction, entry_price, atr_pct, stop_loss '
         'FROM playbook_instructions WHERE direction IN ("BUY","SELL_SHORT") '
         'AND entry_price>0 AND date >= "2026-07-14" AND symbol IN (%s) '
         'ORDER BY date' % ",".join("?" * len(db_syms)))
    insts = [dict(r) for r in c.execute(q, db_syms)]
    print(f"指令池: {len(insts)} 条 (有1分K覆盖票, 7-14起)")

    bars_cache = {}
    results = []
    results_online = []  # §二十九条件4: 线上真门基线 (量能0.8/低ATR3%/gap8%)
    skipped_no_bars = 0
    for inst in insts:
        sym = inst["symbol"]
        if sym not in bars_cache:
            bars_cache[sym] = load_bars_1m(sym)
        bars = bars_cache[sym]
        if not bars:
            skipped_no_bars += 1
            continue
        by_date = group_by_date(bars)
        r = simulate_instruction(inst, by_date, sorted(by_date.keys()))
        if r is None:
            skipped_no_bars += 1
            continue
        r["inst_date"] = inst["date"][:10]
        results.append(r)
        ro = simulate_instruction(inst, by_date, sorted(by_date.keys()),
                                  online_gates=True)
        if ro is not None and "online_gate_skip" not in ro:
            ro["inst_date"] = inst["date"][:10]
            results_online.append(ro)

    # ── 汇总 ──
    def stats(key, pool):
        rows = [r[key] for r in pool if r[key]["triggered"]]
        n = len(rows)
        if not n:
            return {"n": 0}
        mfe = sorted(x["mfe_pct"] for x in rows)
        mae = sorted(x["mae_pct"] for x in rows)
        pnl = [x["path_pnl_pct"] for x in rows]
        slt = [x["sl_touch"] for x in rows if x["sl_touch"] is not None]
        return {
            "n": n,
            "trigger_rate_pct": pct(n / len(pool)),
            "pullback_pct": pct(sum(1 for x in rows if x["path"] == "pullback") / n),
            "chase_pct": pct(sum(1 for x in rows if x["path"] == "chase") / n),
            "mfe_med_pct": round(mfe[n // 2], 2),
            "mae_med_pct": round(mae[n // 2], 2),
            "path_pnl_sum_pct": round(sum(pnl), 1),
            "path_pnl_med_pct": round(sorted(pnl)[n // 2], 2),
            "win_rate_pct": pct(sum(1 for x in pnl if x > 0) / n),
            "sl_touch_rate_pct": pct(sum(1 for x in slt if x) / len(slt)) if slt else None,
        }

    # 配对差异: flat触发但vol没触发 (vol拒掉的) / vol触发但flat没触发 (vol新增的)
    rejected = [r for r in results if r["flat"]["triggered"] and not r["vol"]["triggered"]]
    added = [r for r in results if r["vol"]["triggered"] and not r["flat"]["triggered"]]
    widen_added = [r for r in results if r["vol_widen"]["triggered"] and not r["flat"]["triggered"]]
    widen_lost = [r for r in results if r["flat"]["triggered"] and not r["vol_widen"]["triggered"]]
    # 真基线 (线上门) 的配对分解
    on_added = [r for r in results_online if r["vol_widen"]["triggered"] and not r["flat"]["triggered"]]
    on_lost = [r for r in results_online if r["flat"]["triggered"] and not r["vol_widen"]["triggered"]]
    on_changed = [r for r in results_online if r["flat"]["triggered"] and r["vol_widen"]["triggered"]
                  and r["flat"]["trigger_price"] != r["vol_widen"]["trigger_price"]]

    def group_quality(rows, key):
        pnl = [r[key]["path_pnl_pct"] for r in rows if r[key]["triggered"]]
        if not pnl:
            return None
        return {"n": len(pnl), "sum_pct": round(sum(pnl), 1),
                "med_pct": round(sorted(pnl)[len(pnl) // 2], 2),
                "win_rate_pct": pct(sum(1 for x in pnl if x > 0) / len(pnl))}

    def changed_quality(rows):
        """变更触发通道 (F1): flat与widen都触发但触发价不同."""
        fp = [r["flat"]["path_pnl_pct"] for r in rows]
        wp = [r["vol_widen"]["path_pnl_pct"] for r in rows]
        if not fp:
            return None
        return {"n": len(rows), "flat_sum_pct": round(sum(fp), 1),
                "widen_sum_pct": round(sum(wp), 1),
                "net_change_pct": round(sum(wp) - sum(fp), 1)}

    out = {
        "meta": {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "universe": f"{len(results)} 指令 (1分K覆盖票, 2026-07-14起)",
            "skipped_no_bars": skipped_no_bars,
            "flat": "knife=3%/chase=5% (线上常量)",
            "vol": "knife=clamp(0.9*ATR%,2,6)/chase=clamp(1.5*ATR%,3,9)",
            "trigger_window": "ET 09:35-14:00",
            "note": "触发价=bar收盘价近似(线上用bid/ask); 路径=当日剩余不隔夜; 量能用1分K自聚合5日均量",
        },
        "flat": stats("flat", results),
        "vol": stats("vol", results),
        "vol_widen": stats("vol_widen", results),
        "diff": {
            "rejected_by_vol": {
                "n": len(rejected),
                "flat_path_quality": group_quality(rejected, "flat"),
            },
            "added_by_vol": {
                "n": len(added),
                "vol_path_quality": group_quality(added, "vol"),
            },
            "widen_vs_flat": {
                "added": {"n": len(widen_added),
                          "quality": group_quality(widen_added, "vol_widen")},
                "lost": {"n": len(widen_lost),
                         "quality": group_quality(widen_lost, "flat")},
                "changed_trigger": changed_quality(
                    [r for r in results if r["flat"]["triggered"] and r["vol_widen"]["triggered"]
                     and r["flat"]["trigger_price"] != r["vol_widen"]["trigger_price"]]),
            },
        },
        "online_gates_baseline": {
            "note": "§二十九条件4真基线: 量能门0.8(unified_config:1065)+低ATR门3%(RFC-060)+extreme_gap8%(pipeline L4466)",
            "universe": f"{len(results_online)} 指令 (剔除低ATR<3%)",
            "flat": stats("flat", results_online),
            "vol_widen": stats("vol_widen", results_online),
            "widen_vs_flat": {
                "added": {"n": len(on_added),
                          "quality": group_quality(on_added, "vol_widen")},
                "lost": {"n": len(on_lost),
                         "quality": group_quality(on_lost, "flat")},
                "changed_trigger": changed_quality(on_changed),
            },
        },
        "per_instruction": results,
    }
    outp = DATA / "entry_band_replay_latest.json"
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "per_instruction"},
                     ensure_ascii=False, indent=1))
    print(f"\n落盘: {outp}")


if __name__ == "__main__":
    main()
