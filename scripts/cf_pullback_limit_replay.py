#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PULLBACK_LIMIT/FADE_SHORT 影子次日重放 (DEB-20260901 Phase B).

口径: 对带结构化入场语义 (entry_archetype + confirmation) 的 DA 指令,
重放"若按确认位限价入场 (而非现状市价穿越)"的反事实路径:
  - 确认位在 TTL 窗内触及 → 成交价 = min(确认位, bar开盘) [BUY] / max [SHORT]
  - TTL 内未触及 → EXPIRED_UNFILLED (不进场, 机会成本用当日实际成交对照)
  - 出场: 先判指令 SL 触及, 否则 EOD 收盘价
  - 量价条件 (confirmation 仅价格+TTL) 未重放 → 结果为价格条件下界
数据源:
  - playbook_instructions.raw_instruction_json (archetype/confirmation)
  - data/shadow/pullback_limit_shadow.jsonl (触发上下文, 可选)
  - data/replay_bars/<SYM>_<date>.json ([time,o,h,l,c])
  - trade_records (实际执行结果对照)
用法: python3 scripts/cf_pullback_limit_replay.py [--date YYYY-MM-DD] [--json out]
      缺省 date = 上一交易日 (简单回退: 昨天, 周一跑周日无 bar 自然跳过)。
"""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
BAR_DIR = REPO / "data" / "replay_bars"
DB = REPO / "data" / "ftseek_us.db"
SHADOW = REPO / "data" / "shadow" / "pullback_limit_shadow.jsonl"
OUT_JSONL = REPO / "data" / "shadow" / "pullback_limit_replay.jsonl"
OUT_LATEST = REPO / "data" / "shadow" / "pullback_limit_replay_latest.json"


def load_bars(sym: str, date: str):
    p = BAR_DIR / f"{sym.replace('.', '_')}_{date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _loads(line):
    try:
        return json.loads(line)
    except Exception:
        return None


def load_shadow_rows(date: str):
    out, bad = {}, 0
    if not SHADOW.exists():
        return out
    for line in SHADOW.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = _loads(line)
        if r is None:
            bad += 1
            continue
        if r.get("date") != date:
            continue
        out.setdefault(r.get("inst_id"), {})[r.get("type", "trigger")] = r
    if bad:
        print(f"WARNING: pullback_limit_shadow.jsonl 有 {bad} 行解析失败已跳过")
    return out


def replay_one(sym, date, bars, direction, confirm, sl, qty, start_hm):
    """返回 dict: status/cf_entry/cf_entry_t/cf_exit/cf_exit_why/cf_pnl."""
    lvl = confirm.get("price_level")
    side = confirm.get("side") or ("above" if direction == "SHORT" else "below")
    try:
        ttl = int(confirm.get("ttl_min") or 15)
    except Exception:
        ttl = 15
    if not lvl or qty <= 0:
        return {"status": "skip_no_level"}
    sign = -1.0 if direction == "SHORT" else 1.0
    # TTL 窗: 从 start_hm 起 ttl 分钟 (start 缺省 09:31)
    def _hm(b):
        return b[0][11:16]
    end_h = int(start_hm[:2]) * 60 + int(start_hm[3:5]) + ttl
    end_hm = f"{end_h // 60:02d}:{end_h % 60:02d}"
    entry = None
    for b in bars:
        if _hm(b) < start_hm or _hm(b) >= end_hm:
            if entry is None and _hm(b) >= end_hm:
                break
            if entry is None:
                continue
        if side == "below" and b[3] <= lvl:
            entry = (_hm(b), min(lvl, b[1]))
            break
        if side == "above" and b[2] >= lvl:
            entry = (_hm(b), max(lvl, b[1]))
            break
    if entry is None:
        return {"status": "expired_unfilled", "confirm_level": lvl}
    et, ep = entry
    exit_px, exit_why = None, None
    if sl:
        for b in bars:
            if _hm(b) <= et:
                continue
            if direction == "SHORT" and b[2] >= sl:
                exit_px, exit_why = max(sl, b[1]), "SL"
                break
            if direction != "SHORT" and b[3] <= sl:
                exit_px, exit_why = min(sl, b[1]), "SL"
                break
    if exit_px is None:
        exit_px, exit_why = bars[-1][4], "EOD"
    cf_pnl = (exit_px - ep) * qty * sign
    return {"status": "entered", "confirm_level": lvl, "cf_entry": round(ep, 4),
            "cf_entry_t": et, "cf_exit": round(exit_px, 4), "cf_exit_why": exit_why,
            "cf_pnl": round(cf_pnl, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    date = args.date or (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    shadow = load_shadow_rows(date)

    rows = db.execute(
        """SELECT i.id, i.symbol, i.direction, i.entry_price, i.stop_loss,
                  i.take_profit, i.position_size, i.decision_id, i.raw_instruction_json,
                  t.pnl_net AS actual_net, t.quantity AS actual_qty, t.exit_reason
           FROM playbook_instructions i
           LEFT JOIN trade_records t
             ON t.id = (SELECT MIN(t2.id) FROM trade_records t2
                        WHERE t2.decision_id = i.decision_id AND i.decision_id != ''
                          AND t2.source_type = 'PLAYBOOK_SIM')
           WHERE i.date = ? AND i.direction IN ('BUY','SELL_SHORT')""", (date,)).fetchall()

    results = []
    bad_raw = 0
    for r in rows:
        raw = _loads(r["raw_instruction_json"] or "{}")
        if raw is None:
            bad_raw += 1
            continue
        arch = raw.get("entry_archetype")
        if not arch:
            continue  # 旧指令无结构化语义, 不进池
        conf = raw.get("confirmation") or {}
        sym = r["symbol"]
        bars = load_bars(sym, date)
        direction = "SHORT" if r["direction"] == "SELL_SHORT" else "LONG"
        qty = r["actual_qty"] or r["position_size"] or 0
        sh = shadow.get(r["id"], {})
        trig = sh.get("trigger") or {}
        start_hm = "09:31"
        ts = trig.get("ts") or ""
        if ts:
            try:
                start_hm = datetime.fromisoformat(ts).strftime("%H:%M")
            except Exception:
                start_hm = "09:31"  # ts 异常回落开盘窗起点 (保守)
        if bars is None:
            results.append({"date": date, "symbol": sym, "inst_id": r["id"],
                            "entry_archetype": arch, "status": "no_bars"})
            continue
        rep = replay_one(sym, date, bars, direction, conf,
                         r["stop_loss"], qty, start_hm)
        results.append({
            "date": date, "symbol": sym, "inst_id": r["id"],
            "decision_id": r["decision_id"], "direction": direction,
            "entry_archetype": arch,
            "chase_cap_price": raw.get("chase_cap_price"),
            "chase_cap_violated": bool(sh.get("chase_cap_violation")),
            "actual_executed": r["actual_net"] is not None,
            "actual_net": r["actual_net"],
            "actual_exit_reason": r["exit_reason"],
            **rep,
        })

    n = len(results)
    entered = [x for x in results if x.get("status") == "entered"]
    expired = [x for x in results if x.get("status") == "expired_unfilled"]
    executed = [x for x in results if x.get("actual_executed")]
    summary = {
        "date": date, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "n_instructions_with_archetype": n,
        "n_cf_entered": len(entered), "n_expired_unfilled": len(expired),
        "n_no_bars": sum(1 for x in results if x.get("status") == "no_bars"),
        "n_actually_executed": len(executed),
        "actual_net_sum": round(sum(x["actual_net"] for x in executed), 2),
        "cf_net_sum": round(sum(x["cf_pnl"] for x in entered), 2),
        "n_chase_cap_violated": sum(1 for x in results if x.get("chase_cap_violated")),
        "note": "价格条件下界; 量价条件未重放; cf 出场仅 SL/EOD 近似",
    }
    with OUT_JSONL.open("a", encoding="utf-8") as f:
        for x in results:
            f.write(json.dumps(x, ensure_ascii=False) + "\n")
    OUT_LATEST.write_text(json.dumps({"summary": summary, "rows": results},
                                     ensure_ascii=False, indent=1))
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    for x in results:
        st = x.get("status")
        tag = {"entered": f"CF入场{x.get('cf_entry')}@{x.get('cf_entry_t')} "
                          f"出场{x.get('cf_exit')}({x.get('cf_exit_why')}) CF净={x.get('cf_pnl')}",
               "expired_unfilled": f"确认位{x.get('confirm_level')} TTL内未触及→不进场",
               "no_bars": "无bar数据", "skip_no_level": "无确认位"}.get(st, st)
        act = f"实际净={x['actual_net']}({x.get('actual_exit_reason')})" if x.get("actual_executed") else "实际未成交"
        print(f"{x['symbol']} {x.get('entry_archetype')}: {tag} | {act}")
    if args.json:
        Path(args.json).write_text(json.dumps(results, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
