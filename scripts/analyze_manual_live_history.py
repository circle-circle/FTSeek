"""Pair REAL-account history deals into FIFO round trips and summarize manual trading performance.

Input: data/manual_live_history_2026-08-15.json (pull_manual_live_history.py)
Output: data/manual_live_roundtrips_2026-08-15.json + stdout summary. Gross P&L (deals carry no fee fields).
"""
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
IN = REPO / "data" / "manual_live_history_2026-08-15.json"
OUT = REPO / "data" / "manual_live_roundtrips_2026-08-15.json"


def parse_ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)


def main():
    deals = json.loads(IN.read_text())["deals"]
    us = [d for d in deals if str(d.get("code", "")).startswith("US.") and str(d.get("status")) in ("FILLED_ALL", "None", "")]
    # keep all statuses except explicitly cancelled (history deals are fills by definition)
    us = [d for d in deals if str(d.get("code", "")).startswith("US.")]
    us.sort(key=lambda d: parse_ts(d["create_time"]))

    by_sym = defaultdict(list)
    for d in us:
        by_sym[str(d["code"])].append(d)

    trips = []
    for sym, ds in by_sym.items():
        long_q = []   # open BUY lots
        short_q = []  # open SELL_SHORT lots
        for d in ds:
            side = str(d["trd_side"])
            qty = float(d["qty"])
            px = float(d["price"])
            ts = parse_ts(d["create_time"])
            if side == "BUY":
                # close shorts first
                while qty > 0 and short_q:
                    lot = short_q[0]
                    take = min(qty, lot["qty"])
                    trips.append(dict(symbol=sym, direction="SHORT", qty=take,
                                      entry_time=lot["ts"].isoformat(), exit_time=ts.isoformat(),
                                      entry_price=lot["px"], exit_price=px,
                                      gross_pnl=(lot["px"] - px) * take,
                                      hold_h=(ts - lot["ts"]).total_seconds() / 3600))
                    lot["qty"] -= take
                    qty -= take
                    if lot["qty"] <= 1e-9:
                        short_q.pop(0)
                if qty > 0:
                    long_q.append(dict(qty=qty, px=px, ts=ts))
            elif side == "SELL":
                while qty > 0 and long_q:
                    lot = long_q[0]
                    take = min(qty, lot["qty"])
                    trips.append(dict(symbol=sym, direction="LONG", qty=take,
                                      entry_time=lot["ts"].isoformat(), exit_time=ts.isoformat(),
                                      entry_price=lot["px"], exit_price=px,
                                      gross_pnl=(px - lot["px"]) * take,
                                      hold_h=(ts - lot["ts"]).total_seconds() / 3600))
                    lot["qty"] -= take
                    qty -= take
                    if lot["qty"] <= 1e-9:
                        long_q.pop(0)
                if qty > 0:  # SELL acting as short open (mislabeled) — treat as short open
                    short_q.append(dict(qty=qty, px=px, ts=ts))
            elif side == "SELL_SHORT":
                short_q.append(dict(qty=qty, px=px, ts=ts))
            elif side == "BUY_BACK":
                while qty > 0 and short_q:
                    lot = short_q[0]
                    take = min(qty, lot["qty"])
                    trips.append(dict(symbol=sym, direction="SHORT", qty=take,
                                      entry_time=lot["ts"].isoformat(), exit_time=ts.isoformat(),
                                      entry_price=lot["px"], exit_price=px,
                                      gross_pnl=(lot["px"] - px) * take,
                                      hold_h=(ts - lot["ts"]).total_seconds() / 3600))
                    lot["qty"] -= take
                    qty -= take
                    if lot["qty"] <= 1e-9:
                        short_q.pop(0)
                if qty > 0:
                    long_q.append(dict(qty=qty, px=px, ts=ts))
        # residual open lots noted
        for lot in long_q:
            trips.append(dict(symbol=sym, direction="LONG_OPEN_RESIDUAL", qty=lot["qty"],
                              entry_time=lot["ts"].isoformat(), exit_time=None,
                              entry_price=lot["px"], exit_price=None, gross_pnl=None, hold_h=None))
        for lot in short_q:
            trips.append(dict(symbol=sym, direction="SHORT_OPEN_RESIDUAL", qty=lot["qty"],
                              entry_time=lot["ts"].isoformat(), exit_time=None,
                              entry_price=lot["px"], exit_price=None, gross_pnl=None, hold_h=None))

    closed = [t for t in trips if t["gross_pnl"] is not None]
    OUT.write_text(json.dumps({"n_roundtrips": len(closed), "trips": trips}, ensure_ascii=False, indent=1))

    n = len(closed)
    total = sum(t["gross_pnl"] for t in closed)
    wins = [t for t in closed if t["gross_pnl"] > 0]
    losses = [t for t in closed if t["gross_pnl"] <= 0]
    print(f"=== 实盘人工回合 (US, FIFO, 毛口径未扣费) ===")
    print(f"n={n}  total={total:+.2f}  WR={100*len(wins)/n:.1f}%  avg={total/n:+.2f}")
    print(f"avg_win={sum(t['gross_pnl'] for t in wins)/max(len(wins),1):+.2f}  avg_loss={sum(t['gross_pnl'] for t in losses)/max(len(losses),1):+.2f}")
    for dr in ("LONG", "SHORT"):
        sub = [t for t in closed if t["direction"] == dr]
        if sub:
            st = sum(t["gross_pnl"] for t in sub)
            sw = sum(1 for t in sub if t["gross_pnl"] > 0)
            print(f"{dr}: n={len(sub)} total={st:+.2f} WR={100*sw/len(sub):.1f}% avg={st/len(sub):+.2f}")
    # concentration
    by_sym_pnl = defaultdict(float)
    for t in closed:
        by_sym_pnl[t["symbol"]] += t["gross_pnl"]
    top = sorted(by_sym_pnl.items(), key=lambda kv: -abs(kv[1]))[:8]
    print("symbol concentration:", [(s, round(p, 1)) for s, p in top])
    # holding period
    import statistics
    holds = [t["hold_h"] for t in closed]
    print(f"hold hours: median={statistics.median(holds):.1f} mean={sum(holds)/len(holds):.1f}")
    day = [t for t in closed if t["entry_time"][:10] == t["exit_time"][:10]]
    ovn = [t for t in closed if t["entry_time"][:10] != t["exit_time"][:10]]
    if day and ovn:
        print(f"day-trade n={len(day)} total={sum(t['gross_pnl'] for t in day):+.2f} | overnight n={len(ovn)} total={sum(t['gross_pnl'] for t in ovn):+.2f}")
    # monthly
    bym = defaultdict(float)
    for t in closed:
        bym[t["exit_time"][:7]] += t["gross_pnl"]
    print("monthly:", {k: round(v, 1) for k, v in sorted(bym.items())})


if __name__ == "__main__":
    main()
