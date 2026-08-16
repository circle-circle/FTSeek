#!/usr/bin/env python3
"""锚限价影子次日重放器 (DEB-20260806 终裁"同意", 2026-08-06).

读 data/limit_entry_shadow.jsonl (触发时落, record-only), 用 1-min bar 按修正口径
重放"若挂 anchor 限价 + 追单上限"的配对反事实, 落逐行判定 + 汇总, 供主门读数。

修正口径 (辩论§4.2/§4.3 终裁条文):
  - 触发 bar 内不判成交 (分钟末标签, bar open 是触发前印价 → 物理不可能);
    触发 bar 标签 = ceil(触发时刻到分钟末)。
  - δ 完整定义: anchor 成交 → sign×(实际入场−anchor)×qty (按限价定价, 保守);
    追单成交 → sign×(实际入场−追单价)×qty (可负); 漏单 → δ=−实际净 (可负)。
  - 市价化笔 (anchor 限价 ≥ 触发市价) δ≡0, 记 bucket=marketable, 不入主门人群。
  - 追单阶梯: N min 未成交 → 改挂带边缘 (entry_high LONG/entry_low SHORT, 市价化)
    → 下一根 bar open 成交; N∈{5,30} 两档。
  - 决策量用毛差 (配对同量同退出, 费用自然消去)。

幂等: 全量重建, 每次跑重写全部输出; (inst_id,date) 首行去重; 未平仓配对自动 pending。
对账哨兵: 每日影子行数 vs entry_timing_shadow live_triggered=true 行数, 不等即在汇总告警。

调度: 系统 crontab `30 5 * * 2-6` (CST 05:30 周二~六 = ET 17:30 周一~五)。只读, 零交易变更。
"""
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import pytz  # noqa: E402
import limit_entry_counterfactual as lec  # noqa: E402  (复用 try_fill/get_bars, 一字不改)

ET = pytz.timezone("America/New_York")
SHADOW_PATH = REPO / "data" / "limit_entry_shadow.jsonl"
OUT_JSONL = REPO / "data" / "limit_entry_shadow_replay.jsonl"
OUT_SUMMARY = REPO / "data" / "limit_entry_shadow_replay_latest.json"
ENTRY_TIMING_PATH = REPO / "data" / "entry_timing_shadow.jsonl"
CHASE_NS = (5, 30)


def _parse_dt_et(raw):
    """三格式混杂 → tz-aware ET (erb bar 时间是 aware, 比较必须同型)."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except Exception:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(ET)
    if 4 <= dt.hour < 20:
        return ET.localize(dt)
    return ET.localize(dt - timedelta(hours=12))


def _trigger_bar_end(dt):
    """分钟末标签口径: 触发 bar 标签 = ceil(触发时刻到分钟末)."""
    base = dt.replace(second=0, microsecond=0)
    if dt.second or dt.microsecond:
        base += timedelta(minutes=1)
    return base


def load_shadow():
    if not SHADOW_PATH.exists():
        return []
    seen, rows = set(), []
    with SHADOW_PATH.open(encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except Exception:
                continue
            key = (r.get("inst_id"), r.get("date"))
            if key in seen or not r.get("anchor"):
                continue
            seen.add(key)
            rows.append(r)
    return rows


def load_trades_by_decision():
    from app.core.paths import get_trade_records_db
    conn = sqlite3.connect(f"file:{get_trade_records_db()}?mode=ro", uri=True)
    out = {}
    for r in conn.execute(
        """SELECT decision_id, symbol, direction, entry_price, exit_price, quantity,
                  entry_time, exit_time, pnl_net
           FROM trade_records WHERE decision_id IS NOT NULL AND decision_id != ''"""
    ):
        out.setdefault(r[0], []).append({
            "symbol": r[1], "direction": r[2], "entry": r[3], "exit": r[4],
            "qty": r[5] or 1, "entry_time": r[6], "exit_time": r[7], "net": r[8] or 0.0,
        })
    conn.close()
    return out


def judge(row, trade, bars, chase_n):
    """单行判定. 返回 dict (filled/bucket/delay_min/improvement/delta/reason)."""
    long = row["direction"] == "LONG"
    sign = 1.0 if long else -1.0
    anchor = float(row["anchor"])
    band_edge = float(row["entry_high"]) if long else float(row["entry_low"])
    trigger_dt = _parse_dt_et(row["ts"])
    qty = int(trade["qty"]) if trade else int(row.get("position_size") or 1)

    if trade:
        actual_entry = float(trade["entry"])
        actual_net = float(trade["net"])
        ref_dt = _parse_dt_et(trade["entry_time"]) or trigger_dt
        exit_dt = _parse_dt_et(trade["exit_time"])
    else:
        actual_entry = float(row["trigger_price"])
        actual_net = 0.0
        ref_dt = trigger_dt
        exit_dt = trigger_dt.replace(hour=16, minute=0, second=0)
    if exit_dt is None:
        exit_dt = ref_dt.replace(hour=16, minute=0, second=0)

    marketable = (anchor >= actual_entry) if long else (anchor <= actual_entry)
    if marketable:
        return {"filled": True, "bucket": "marketable", "delay_min": 0.0,
                "improvement": 0.0, "delta": 0.0, "reason": "marketable_cap"}

    trig_end = _trigger_bar_end(ref_dt)
    chase_end = ref_dt + timedelta(minutes=chase_n)
    window = [b for b in bars if trig_end < b["t"] <= exit_dt]

    # 阶段1: anchor 限价, [触发bar之后, chase_end]
    for b in window:
        if b["t"] > chase_end:
            break
        touch = (b["l"] <= anchor) if long else (b["h"] >= anchor)
        if touch:
            delay = (b["t"] - ref_dt).total_seconds() / 60.0
            impr = sign * (actual_entry - anchor) * qty  # 按限价定价 (保守)
            return {"filled": True, "bucket": "passive_anchor", "delay_min": round(delay, 2),
                    "improvement": round(impr, 4), "delta": round(impr, 4),
                    "penetration": round((anchor - b["l"]) if long else (b["h"] - anchor), 4),
                    "reason": f"anchor_fill_{chase_n}m"}
    # 阶段2: 追单改挂带边缘 (市价化) → 其后首根 bar open 成交
    for b in window:
        if b["t"] <= chase_end:
            continue
        chase_px = float(b["o"]) if float(b["o"] or 0) > 0 else band_edge
        delta = sign * (actual_entry - chase_px) * qty  # 可负
        delay = (b["t"] - ref_dt).total_seconds() / 60.0
        return {"filled": True, "bucket": "chase_fill", "delay_min": round(delay, 2),
                "improvement": round(delta, 4), "delta": round(delta, 4),
                "chase_px": chase_px, "reason": f"chase_{chase_n}m"}
    # 阶段3: 漏单
    delta = -actual_net if trade else 0.0
    return {"filled": False, "bucket": "missed", "delay_min": None,
            "improvement": 0.0, "delta": round(delta, 4),
            "reason": f"miss_{chase_n}m"}


def main():
    shadow = load_shadow()
    trades = load_trades_by_decision()
    print(f"[replay] 影子行 {len(shadow)} (去重后)")

    if not shadow:
        OUT_SUMMARY.write_text(json.dumps({"n_shadow": 0, "note": "无影子行"}, ensure_ascii=False))
        return 0

    from futu import OpenQuoteContext
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    results = []
    try:
        for row in shadow:
            tr_list = trades.get(row.get("decision_id") or "", [])
            trade = None
            for t in tr_list:
                if t["symbol"].endswith(row["symbol"].replace("US.", "")) or t["symbol"] == row["symbol"]:
                    trade = t
                    break
            trigger_dt = _parse_dt_et(row["ts"])
            if trade and _parse_dt_et(trade["exit_time"]):
                end_dt = _parse_dt_et(trade["exit_time"])
            else:
                end_dt = trigger_dt.replace(hour=16, minute=0, second=0)
            bars = lec.get_bars(ctx, row["symbol"], trigger_dt, end_dt) if trigger_dt else []
            rec = {"inst_id": row.get("inst_id"), "decision_id": row.get("decision_id"),
                   "symbol": row["symbol"], "direction": row["direction"],
                   "date": row.get("date"), "anchor": row["anchor"],
                   "trigger_price": row.get("trigger_price"), "entry_path": row.get("entry_path"),
                   "paired_trade": bool(trade), "actual_net": (trade or {}).get("net"),
                   "n_bars": len(bars)}
            if not bars:
                rec["judge"] = {"filled": None, "bucket": "no_bars", "delta": None}
            else:
                for n in CHASE_NS:
                    rec[f"judge_{n}m"] = judge(row, trade, bars, n)
            results.append(rec)
    finally:
        ctx.close()

    with OUT_JSONL.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 汇总 (主门读数直读): 真被动人群 = bucket != marketable
    summary = {"generated_at": datetime.now(ET).isoformat(timespec="seconds"),
               "n_shadow": len(results), "variants": {}}
    for n in CHASE_NS:
        key = f"judge_{n}m"
        judged = [r for r in results if r.get(key)]
        passive = [r for r in judged if r[key].get("bucket") != "marketable"]
        paired = [r for r in passive if r["paired_trade"]]
        deltas = [r[key]["delta"] for r in paired if r[key].get("delta") is not None]
        delays = [r[key]["delay_min"] for r in paired if r[key].get("delay_min") is not None]
        delays.sort()
        summary["variants"][f"chase_{n}m"] = {
            "n_judged": len(judged),
            "n_passive_population": len(passive),
            "n_paired": len(paired),
            "fill_rate": round(sum(1 for r in paired if r[key].get("filled")) / max(len(paired), 1), 4),
            "delta_sum": round(sum(deltas), 2),
            "delta_mean": round(sum(deltas) / max(len(deltas), 1), 4),
            "delta_neg_n": sum(1 for d in deltas if d < 0),
            "delay_p50": delays[len(delays) // 2] if delays else None,
            "delay_max": delays[-1] if delays else None,
            "buckets": {b: sum(1 for r in paired if r[key].get("bucket") == b)
                        for b in ("passive_anchor", "chase_fill", "missed", "no_bars")},
        }
    # 对账哨兵: 影子行数 vs entry_timing_shadow live_triggered=true (按日)
    try:
        recon = {}
        for r in results:
            recon.setdefault(r["date"], {"shadow": 0, "entry_timing": 0})
            recon[r["date"]]["shadow"] += 1
        if ENTRY_TIMING_PATH.exists():
            with ENTRY_TIMING_PATH.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except Exception:
                        continue
                    d = (e.get("ts") or "")[:10]
                    if d in recon and e.get("live_triggered"):
                        recon[d]["entry_timing"] += 1
        summary["reconciliation"] = {d: v for d, v in recon.items() if v["shadow"] != v["entry_timing"]}
        summary["reconciliation_ok"] = not summary["reconciliation"]
    except Exception as e:
        summary["reconciliation_error"] = str(e)

    OUT_SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"→ {OUT_JSONL} / {OUT_SUMMARY}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
