#!/usr/bin/env python3
"""退出规则「禁用型」反事实影子 (M2, 2026-09-04) — MFE_STALL_EXIT / DEAD_SIGNAL_EXIT 关掉会怎样。

动机 (复盘 2026-09-03): 日报 exit_reason_pnl 对 MFE_STALL_EXIT 给 🚨disable、
  DEAD_SIGNAL_EXIT 给 ⚠review, 但那只是【实现值】口径 — 触发时已亏 ≠ 禁用更好,
  没有反事实 (不触发会落到哪: SL_INITIAL 深亏 / 回本 / EOD?) 直接禁用属
  「拿实现值当反事实」的尺子歪风险。本脚本用当日 1 分 bar (data/replay_bars,
  端标) 对每笔此类出场回放【若不在此出场、按现行剩余规则链持有到收盘】的结局:
    cf 链 = β收紧初始SL → BE/dd 锁利 (be0.4/dd0.8, 触发线垫 fee+slip≈0.35%)
            → 当日收盘市价 (TIME_EXIT 未回放: 因子级 hours 不可得; 隔夜后续日不回放)
  saved = cf - live (双边同一次往返, 费用相消)。saved>0 = 禁用该规则更优。
零行为变更, 只读 DB + bar 文件; 幂等 (按 trade_id 去重, 可重复跑/补跑)。

运行: ./venv/bin/python scripts/exit_disable_cf_shadow.py [--days 60] [--date YYYY-MM-DD]
输出: data/shadow/exit_disable_cf.jsonl (逐笔) + exit_disable_cf_latest.json (汇总)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from datetime import date, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DB = REPO / "data" / "ftseek_us.db"
BAR_DIR = REPO / "data" / "replay_bars"
OUT = REPO / "data" / "shadow" / "exit_disable_cf.jsonl"
OUT_LATEST = REPO / "data" / "shadow" / "exit_disable_cf_latest.json"

RULES = ("MFE_STALL_EXIT", "DEAD_SIGNAL_EXIT")
MIN_N = 50
EXHAUSTION_TRADING_DAYS = 20
# 反事实链参数 (对齐现行 default 因子规则 + 生产保本触发线近似):
#   be=0.4 (playbook_horizon default, 2026-08-17), dd=0.8, 初始SL经 tighten_sl_by_beta
#   (β=0.5 下限1.5%, 与实盘同源); 触发线垫 = fee-aware(~0.147%)+slip(0.2%) ≈ 0.35%。
CF_BE_PCT = 0.4
CF_DD_PCT = 0.8
CF_PAD_PCT = 0.35


def _variant_eff_sl(direction, entry, initial_sl, peak_profit_pct, be_pct, dd_pct, pad_pct):
    """复刻 exit_shadow._variant_effective_sl 数学 (be→BE线含垫; peak>be 后 dd 锁利)。"""
    eff = initial_sl
    if peak_profit_pct < be_pct:
        return eff
    if direction == "LONG":
        eff = max(eff, entry * (1 + pad_pct / 100.0))
    else:
        _be = entry * (1 - pad_pct / 100.0)
        eff = min(eff if eff > 0 else _be, _be)
    if dd_pct > 0 and peak_profit_pct > be_pct:
        if direction == "LONG":
            dd_sl = entry * (1 + peak_profit_pct / 100.0) * (1 - dd_pct / 100.0)
            eff = max(eff, dd_sl)
        else:
            dd_sl = entry * (1 - peak_profit_pct / 100.0) * (1 + dd_pct / 100.0)
            eff = min(eff if eff > 0 else entry, dd_sl)
    return eff


def _load_bars(symbol: str, et_date: str):
    code = symbol if symbol.startswith("US.") else f"US.{symbol}"
    p = BAR_DIR / f"{code.replace('.', '_')}_{et_date}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _hm_plus(hm: str, add_min: int = 1) -> str:
    m = int(hm[:2]) * 60 + int(hm[3:5]) + add_min
    return f"{m // 60:02d}:{m % 60:02d}"


def replay_cf(direction: str, entry: float, initial_sl: float, peak_pct0: float,
              bars_after: list):
    """回放反事实链。bars_after: [time,o,h,l,c] 端标列表 (严格晚于出场分钟)。
    返回 (cf_reason, cf_price) 或 (None, None) 无后续 bar。"""
    peak_pct = float(peak_pct0 or 0)
    sl = initial_sl
    last_close = None
    for b in bars_after:
        o, h, l, c = b[1], b[2], b[3], b[4]
        # 1) 当前 eff_sl 是否被本 bar 触碰 (用上一根 bar 末的 peak — 轮询语义)
        if sl > 0 and ((direction == "LONG" and l <= sl) or (direction == "SHORT" and h >= sl)):
            reason = "CF_SL" if sl <= initial_sl * (1 + 1e-9) and direction == "LONG" else "CF_SL"
            # LONG: eff 递增; 判定是否已越过初始SL (BE/锁利) → 分类
            if direction == "LONG":
                reason = "CF_SL_INITIAL" if abs(sl - initial_sl) < entry * 1e-4 else "CF_BE_OR_LOCK"
            else:
                reason = "CF_SL_INITIAL" if abs(sl - initial_sl) < entry * 1e-4 else "CF_BE_OR_LOCK"
            return reason, sl
        # 2) 更新 peak (本 bar 的极值) → 重算 eff_sl
        if direction == "LONG":
            fav = (h - entry) / entry * 100
        else:
            fav = (entry - l) / entry * 100
        if fav > peak_pct:
            peak_pct = fav
        sl = _variant_eff_sl(direction, entry, initial_sl, peak_pct, CF_BE_PCT, CF_DD_PCT, CF_PAD_PCT)
        last_close = c
    if last_close is None:
        return None, None
    return "CF_EOD_CLOSE", last_close


def _signed_pct(direction, entry, px):
    return ((px - entry) if direction == "LONG" else (entry - px)) / entry * 100


def sample_supply_snapshot(conn, counts, today=None):
    """Classify each rule as active, min-n met, or exhausted with a limited-confidence readout."""
    today = today or date.today()
    result = {}
    for rule in RULES:
        row = conn.execute(
            "SELECT MAX(SUBSTR(CAST(exit_time AS TEXT),1,10)) AS latest "
            "FROM trade_records WHERE exit_reason=? AND exit_time IS NOT NULL",
            (rule,)).fetchone()
        latest = str(row["latest"]) if row and row["latest"] else None
        exhausted_after = None
        try:
            if latest:
                from app.utils.holiday_calendar import nth_trading_day_after
                deadline = nth_trading_day_after(
                    "US", date.fromisoformat(latest), EXHAUSTION_TRADING_DAYS)
                exhausted_after = deadline.isoformat() if deadline else None
        except Exception:
            exhausted_after = None
        exhausted = bool(exhausted_after and today.isoformat() >= exhausted_after)
        n = int(counts.get(rule, 0))
        if n >= MIN_N:
            status = "MIN_N_MET"
        elif exhausted:
            status = "EXHAUSTED_FINAL_READOUT"
        else:
            status = "ACTIVE"
        result[rule] = {
            "n": n, "min_n": MIN_N, "latest_exit_date": latest,
            "exhausted_after": exhausted_after, "exhausted": exhausted,
            "status": status,
            "confidence": "limited" if exhausted and n < MIN_N else "normal",
        }
    return result


def main(days: int = 60, only_date: str | None = None) -> int:
    sys_path_hack = str(REPO)
    import sys
    if sys_path_hack not in sys.path:
        sys.path.insert(0, sys_path_hack)
    # β收紧: 与实盘同源 (单一真源), 失败回退原始 SL
    try:
        from app.playbook.config import tighten_sl_by_beta
    except Exception:
        tighten_sl_by_beta = None

    done = set()
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
                if r.get("type") == "cf" and r.get("trade_id"):
                    done.add(r["trade_id"])
            except Exception as e:
                print(f"[exit-disable-cf] 幂等集合脏行跳过: {e}", file=sys.stderr)

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    q = ("SELECT id, symbol, direction, entry_time, exit_time, entry_price, exit_price, "
         "quantity, exit_reason, realized_pnl, metadata_json FROM trade_records "
         "WHERE exit_reason IN (?, ?) AND exit_time IS NOT NULL "
         "AND entry_time >= date('now', ?)")
    rows = con.execute(q, (*RULES, f"-{days} day")).fetchall()
    if only_date:
        rows = [r for r in rows if str(r["entry_time"])[:10] == only_date]

    n_new = n_no_bars = 0
    results = []
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as fh:
        for r in rows:
            if r["id"] in done:
                continue
            et_date = str(r["exit_time"])[:10]
            try:
                md = json.loads(r["metadata_json"] or "{}")
            except Exception:
                md = {}
            entry = float(r["entry_price"] or 0)
            exit_px = float(r["exit_price"] or 0)
            qty = float(r["quantity"] or 0)
            direction = (r["direction"] or "LONG").upper()
            if entry <= 0 or exit_px <= 0:
                continue
            bars = _load_bars(r["symbol"], et_date)
            if not bars:
                n_no_bars += 1
                fh.write(json.dumps({"type": "cf", "trade_id": r["id"], "symbol": r["symbol"],
                                     "date": et_date, "status": "no_bars"}, ensure_ascii=False) + "\n")
                continue
            exit_hm = str(r["exit_time"])[11:16]
            anchor = _hm_plus(exit_hm, 1)  # 出场分钟 bar 的端标; 其后为反事实路径
            after = [b for b in bars if str(b[0])[11:16] > anchor]
            initial_sl = float(md.get("stop_loss") or 0)
            if initial_sl <= 0:
                n_no_bars += 1
                fh.write(json.dumps({"type": "cf", "trade_id": r["id"], "symbol": r["symbol"],
                                     "date": et_date, "status": "no_initial_sl"}, ensure_ascii=False) + "\n")
                continue
            if tighten_sl_by_beta is not None:
                try:
                    initial_sl = float(tighten_sl_by_beta(direction, entry, initial_sl))
                except Exception as e:
                    # R5 留痕: β收紧失败回退原SL → CF 口径与实盘漂移, 必须可见
                    print(f"[exit-disable-cf] β收紧失败回退原SL ({r['symbol']}): {e}", file=sys.stderr)
            peak0 = float(md.get("mfe_pct") or 0)
            reason, cf_px = replay_cf(direction, entry, initial_sl, peak0, after)
            live_pct = _signed_pct(direction, entry, exit_px)
            if cf_px is None:
                rec = {"type": "cf", "trade_id": r["id"], "symbol": r["symbol"], "date": et_date,
                       "status": "no_bars_after", "exit_reason": r["exit_reason"]}
            else:
                cf_pct = _signed_pct(direction, entry, cf_px)
                saved_pct = cf_pct - live_pct
                sign = 1 if direction == "LONG" else -1
                saved_usd = (cf_px - exit_px) * qty * sign if qty > 0 else None
                rec = {
                    "type": "cf", "trade_id": r["id"], "symbol": r["symbol"], "date": et_date,
                    "direction": direction, "exit_reason": r["exit_reason"],
                    "entry": entry, "live_exit": exit_px, "live_pct": round(live_pct, 4),
                    "mfe_at_exit_pct": round(peak0, 4),
                    "cf_reason": reason, "cf_price": round(cf_px, 4),
                    "cf_pct": round(cf_pct, 4),
                    "saved_pct": round(saved_pct, 4),   # >0 = 禁用规则更优 (持有更优)
                    "saved_usd": round(saved_usd, 2) if saved_usd is not None else None,
                    "n_bars_after": len(after),
                    "params": {"be": CF_BE_PCT, "dd": CF_DD_PCT, "pad": CF_PAD_PCT},
                    "note": "cf链=β紧SL→BE/dd→EOD收盘; TIME_EXIT未回放, 隔夜后续日不回放",
                }
                results.append(rec)
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n_new += 1
    con.close()

    # ── 汇总 (读全量 jsonl, 含历史) ──
    all_rows = []
    if OUT.exists():
        for line in OUT.read_text(encoding="utf-8").splitlines():
            try:
                rr = json.loads(line)
                if rr.get("type") == "cf" and rr.get("saved_pct") is not None:
                    all_rows.append(rr)
            except Exception as e:
                print(f"[exit-disable-cf] 汇总读取脏行跳过: {e}", file=sys.stderr)
    supply_conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    supply_conn.row_factory = sqlite3.Row
    supply = sample_supply_snapshot(
        supply_conn,
        {rule: sum(1 for row in all_rows if row.get("exit_reason") == rule) for rule in RULES},
    )
    supply_conn.close()
    summary = {"generated_at": datetime.now().isoformat(timespec="seconds"),
               "n_total_labeled": len(all_rows), "by_rule": {},
               "sample_supply": supply,
               "exhaustion_policy": {
                   "min_n": MIN_N,
                   "consecutive_zero_sample_trading_days": EXHAUSTION_TRADING_DAYS,
                   "action": "close with existing n and limited confidence; no behavior change",
               }}
    for rule in RULES:
        g = [x for x in all_rows if x.get("exit_reason") == rule]
        if not g:
            summary["by_rule"][rule] = {"n": 0}
            continue
        saved = [x["saved_pct"] for x in g]
        usd = [x["saved_usd"] for x in g if x.get("saved_usd") is not None]
        by_reason = {}
        for x in g:
            by_reason[x.get("cf_reason")] = by_reason.get(x.get("cf_reason"), 0) + 1
        summary["by_rule"][rule] = {
            "n": len(g),
            "saved_pct_mean": round(statistics.mean(saved), 4),
            "saved_pct_median": round(statistics.median(saved), 4),
            "hold_better_rate": round(sum(1 for s in saved if s > 0) / len(g), 3),
            "saved_usd_sum": round(sum(usd), 2) if usd else None,
            "cf_reason_dist": by_reason,
        }
    OUT_LATEST.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"[exit-disable-cf] 新增 {n_new} 笔 (no_bars/no_sl {n_no_bars}), 累计标注 {len(all_rows)} 笔")
    for rule, s in summary["by_rule"].items():
        if s.get("n"):
            print(f"  {rule}: n={s['n']} saved中位{s['saved_pct_median']:+.3f}% "
                  f"均值{s['saved_pct_mean']:+.3f}% 持有更优率{s['hold_better_rate']*100:.0f}% "
                  f"saved合计${s['saved_usd_sum']} cf分布{s['cf_reason_dist']}")
        else:
            print(f"  {rule}: 无样本")
    print(f"→ {OUT_LATEST}")
    return 0


if __name__ == "__main__":
    import os
    import sys
    ap = argparse.ArgumentParser(description="MFE_STALL/DEAD_SIGNAL 禁用型反事实回放")
    ap.add_argument("--days", type=int, default=60, help="回看天数 (默认60)")
    ap.add_argument("--date", type=str, default=None, help="仅处理单一 ET 日期")
    a = ap.parse_args()
    rc = main(days=a.days, only_date=a.date)
    sys.stdout.flush()
    os._exit(rc)
