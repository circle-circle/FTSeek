#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_entry_confirm_queue_cost.py — RFC-141 门确认后的「执行队列成本」只读测量.

背景 (2026-10-17 用户讨论): 门检查已是全并发 (全部 arm 后由 10s sweep 独立判定),
真正的串行在 CONFIRMED → 成交之间: 等 180s 主循环 + 单轮 cap=3 排队。
本脚本量化这段队列的成本/收益, 并分离「触发节奏」(标的何时破当日高/低点)
与「队列延迟」(confirm 后等了几轮)。

口径 (方向感知, 与 RFC-145 §2.4 同纪律):
  基准 = "confirm 时刻立即市价成交会拿到的价":
    v2 行有 bid/ask_at_confirm → LONG 用 ask, SHORT 用 bid (真·立即成交对手价);
    v1 行只有 px_at_confirm (last trade) → 口径粗糙, 单列口径标记, 不与 v2 混算中位数。
  imp_bps LONG = (bench - fill)/bench ×1e4 ; SHORT = (fill - bench)/bench ×1e4
  正值 = 队列让成交价比「confirm 时立即成交」更好 (队列是收益); 负值 = 队列是成本。
  lag_sec = fill_time - confirm_ts; lag > 200s ⇒ 至少多等 1 轮 180s 主循环 (cap 排队)。

只读: jsonl + sqlite mode=ro, 零写入。
"""
import json
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JSONL = ROOT / "data" / "shadow" / "entry_confirm_forward.jsonl"
DB = ROOT / "data" / "ftseek_us.db"

LAG_QUEUE_THRESHOLD_S = 200  # 轮间隔 180s + 轮内执行秒数余量


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def main() -> None:
    rows = []
    for line in JSONL.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("type") != "entry_confirm" or r.get("outcome") != "CONFIRMED":
            continue
        if r.get("arm") != "treatment":
            continue
        rows.append(r)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    fills = {}
    for tr in conn.execute(
        "SELECT decision_id, symbol, direction, entry_time, entry_price, quantity, "
        "exit_reason, pnl_net, realized_pnl FROM trade_records "
        "WHERE source_type='PLAYBOOK_SIM'"
    ):
        fills.setdefault(tr["decision_id"], []).append(dict(tr))

    inst_state = {}
    for ins in conn.execute(
        "SELECT decision_id, executed, skip_reason, execution_price, execution_time "
        "FROM playbook_instructions"
    ):
        inst_state[ins["decision_id"]] = dict(ins)
    conn.close()

    table, never_filled = [], []
    for r in sorted(rows, key=lambda x: x["ts"]):
        did = r["decision_id"]
        cands = [f for f in fills.get(did, [])
                 if _parse(f["entry_time"]) >= _parse(r["ts"])]
        if not cands:
            st = inst_state.get(did, {})
            never_filled.append({
                "symbol": r["symbol"], "dir": r["direction"], "confirm_ts": r["ts"],
                "schema": r["schema"], "executed": st.get("executed"),
                "skip_reason": st.get("skip_reason"),
            })
            continue
        f = min(cands, key=lambda x: x["entry_time"])
        confirm_ts, fill_ts = _parse(r["ts"]), _parse(f["entry_time"])
        lag = (fill_ts - confirm_ts).total_seconds()
        # 队列 vs 触发复验的归因: 全链路每日成交密度低, lag>400s 而中间无成交
        # ⇒ 触发闸复验等再突破 (非 cap 排队); 200-400s ⇒ 至多 1-2 轮, cap 排队候选。
        pnl = f.get("pnl_net")
        pnl = float(pnl) if pnl is not None else float(f.get("realized_pnl") or 0)
        is_long = r["direction"] == "LONG"
        v2 = r.get("schema") == "entry_confirm_forward_v2"
        if v2 and r.get("ask_at_confirm") and r.get("bid_at_confirm"):
            bench = r["ask_at_confirm"] if is_long else r["bid_at_confirm"]
            caliber = "ask/bid"
        else:
            bench = r.get("px_at_confirm")
            caliber = "last(v1粗)"
        if not bench or bench <= 0:
            continue
        fill_px = float(f["entry_price"])
        imp_bps = ((bench - fill_px) if is_long else (fill_px - bench)) / bench * 1e4
        imp_usd = ((bench - fill_px) if is_long else (fill_px - bench)) * float(f["quantity"])
        table.append({
            "symbol": r["symbol"], "dir": r["direction"],
            "confirm": confirm_ts.strftime("%m-%d %H:%M:%S"),
            "fill": fill_ts.strftime("%H:%M:%S"), "lag_s": round(lag),
            "bench": bench, "fill_px": fill_px, "imp_bps": round(imp_bps, 1),
            "imp_usd": round(imp_usd, 2), "caliber": caliber,
            "bucket": ("retrigger>400s" if lag > 400
                       else "queued 200-400s" if lag > LAG_QUEUE_THRESHOLD_S
                       else "next-round≤200s"),
            "arm_local": confirm_ts.strftime("%H:%M:%S"),  # confirm 时刻 (ET) 即可定位
            "pnl": round(pnl, 2),
        })

    print(f"CONFIRMED 行 {len(rows)} (v1: {sum(1 for r in rows if r['schema'].endswith('v1'))}, "
          f"v2: {sum(1 for r in rows if r['schema'].endswith('v2'))}) "
          f"→ 成交 {len(table)}, 确认后从未成交 {len(never_filled)}\n")

    print(f"{'symbol':<9}{'dir':<6}{'confirm':<15}{'fill':<10}{'lag_s':>6}"
          f"{'bench':>9}{'fill_px':>9}{'imp_bps':>9}{'imp_$':>8}{'pnl_net':>9}  caliber")
    for t in table:
        print(f"{t['symbol']:<9}{t['dir']:<6}{t['confirm']:<15}"
              f"{t['fill']:<10}{t['lag_s']:>6}{t['bench']:>9.2f}{t['fill_px']:>9.2f}"
              f"{t['imp_bps']:>9.1f}{t['imp_usd']:>8.2f}{t['pnl']:>9.2f}  {t['caliber']}")

    print("\n── 分桶 (imp_bps 正值=延迟是收益; pnl 为全仓净盈亏) ──")
    for bucket in ("next-round≤200s", "queued 200-400s", "retrigger>400s"):
        sub = [t for t in table if t["bucket"] == bucket]
        if not sub:
            continue
        bps = [t["imp_bps"] for t in sub]
        usd = [t["imp_usd"] for t in sub]
        pnls = [t["pnl"] for t in sub]
        better = sum(1 for b in bps if b > 0)
        print(f"{bucket:<18} n={len(sub):>2}  lag中位={statistics.median(t['lag_s'] for t in sub):>5.0f}s"
              f"  imp_bps 中位={statistics.median(bps):>7.1f} 均值={statistics.fmean(bps):>7.1f}"
              f"  改善占比={better}/{len(sub)}  imp_$合计={sum(usd):>+8.2f}"
              f"  交易净pnl合计={sum(pnls):>+8.2f}")

    if table:
        all_bps = [t["imp_bps"] for t in table]
        print(f"\n全量 n={len(table)}  imp_bps 中位={statistics.median(all_bps):.1f}"
              f"  lag 中位={statistics.median(t['lag_s'] for t in table):.0f}s"
              f"  imp_$ 合计={sum(t['imp_usd'] for t in table):+.2f}")

    if never_filled:
        print(f"\n── 确认后从未成交 (队列/后续闸牺牲品, 不计入价格面) ──")
        for nf in never_filled:
            print(f"  {nf['confirm_ts'][:16]} {nf['symbol']:<9}{nf['dir']:<6}"
                  f"executed={nf['executed']} skip={nf['skip_reason']} ({nf['schema'][-2:]})")


if __name__ == "__main__":
    main()
