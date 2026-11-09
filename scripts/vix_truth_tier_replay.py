#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真值 VIX 分层回放 (RFC-190 第三步 · 只读)

问题: RFC-190 §1C 已证「现货>28 的硬门」在 A10 窗口只拦掉最赚钱的一天 (负 edge)。
      但那测的是**二元硬门**。若改成**分级降风险** (高波动档位缩仓), 真值 VIX 是否
      有判别力? 本脚本用已平仓记录 × 环境账本回答, 零写入、零 cron。

口径 (决定结论成立与否, 逐条照抄):
  - 交易集: A10 时代 (trade_date >= 2026-07-01) + data_quality ∈ (clean,real)
            + source_type NOT IN (POSITION_SYNC, MANUAL) —— 直接复用 db_utils.era_clean_where。
  - 「决策时可见的真值」= 该交易日的**上一交易日**收盘 VIX (盘前时点可得), 不是当日收盘。
    当日收盘 VIX 是事后信息, 用它分组 = 前视偏差。
  - 代理口径对照 = trade_records.entry_vix (下单时系统实际看到的代理值), 不重算、不改写。
  - 结果口径 = pnl_net (已扣费用估算); 另印毛额便于看费用影响。

输出: 分层表 + 代理/真值分档不一致天数 + 分级缩仓的粗略算术 (按 pnl_net × 倍数,
      费用不随规模等比 ⇒ 该算术只作上界参考, 报告中须注明局限)。

用法: venv/bin/python scripts/vix_truth_tier_replay.py [--since 2026-07-01] [--json OUT]
"""
import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.db_utils import era_clean_where  # noqa: E402

DB = ROOT / "data" / "ftseek_us.db"
LEDGER = ROOT / "data" / "market_env_daily.jsonl"

BUCKETS: Sequence[Tuple[str, float, float]] = (
    ("<20", 0.0, 20.0),
    ("20-25", 20.0, 25.0),
    ("25-28", 25.0, 28.0),
    (">=28", 28.0, 1e9),
)


def load_ledger() -> Dict[str, dict]:
    if not LEDGER.exists():
        raise SystemExit("环境账本不存在, 先跑 scripts/market_env_ledger.py build")
    return {r["date"]: r for r in
            (json.loads(line) for line in LEDGER.read_text(encoding="utf-8").splitlines() if line)}


def load_trades(since: str) -> List[dict]:
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    where = era_clean_where(conn)
    rows = [dict(r) for r in conn.execute(
        """SELECT trade_date, symbol, direction, entry_session, exit_reason,
                  realized_pnl, pnl_net, fees_est, entry_vix, market_regime
           FROM trade_records
           WHERE trade_date >= ? """ + where +
        " ORDER BY trade_date, symbol", (since,))]
    conn.close()
    return rows


def prior_session_truth(ledger: Dict[str, dict], trade_date: str) -> Optional[dict]:
    """该交易日盘前可见的真值 = 最近一个**早于当日**的账本行 (无则 None)。"""
    prior = [d for d in ledger if d < trade_date]
    if not prior:
        return None
    return ledger[max(prior)]


def bucket_of(value: Optional[float]) -> Optional[str]:
    if value is None:
        return None
    for name, lo, hi in BUCKETS:
        if lo <= value < hi:
            return name
    return None


def summarize(trades: Sequence[dict], key: str) -> List[dict]:
    groups: Dict[str, List[dict]] = {}
    for t in trades:
        groups.setdefault(t.get(key) or "(missing)", []).append(t)
    out = []
    for name, rows in groups.items():
        nets = [float(r.get("pnl_net") or 0.0) for r in rows]
        gross = [float(r.get("realized_pnl") or 0.0) for r in rows]
        wins = sum(1 for n in nets if n > 0)
        out.append({
            "bucket": name,
            "n": len(rows),
            "days": len({r["trade_date"] for r in rows}),
            "win_rate": round(wins / len(rows) * 100, 1),
            "net_sum": round(sum(nets), 2),
            "net_mean": round(sum(nets) / len(rows), 2),
            "net_median": round(statistics.median(nets), 2),
            "gross_sum": round(sum(gross), 2),
        })
    order = {name: i for i, (name, _, _) in enumerate(BUCKETS)}
    out.sort(key=lambda r: order.get(r["bucket"], 99))
    return out


def print_table(title: str, rows: List[dict]) -> None:
    print("\n" + title)
    print("%-10s %5s %5s %7s %11s %10s %10s %11s" %
          ("bucket", "n", "days", "WR%", "net_sum", "net_mean", "net_med", "gross_sum"))
    for r in rows:
        print("%-10s %5d %5d %7.1f %11.2f %10.2f %10.2f %11.2f" %
              (r["bucket"], r["n"], r["days"], r["win_rate"],
               r["net_sum"], r["net_mean"], r["net_median"], r["gross_sum"]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-07-01")
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    ledger = load_ledger()
    trades = load_trades(args.since)
    if not trades:
        print("窗口内无已平仓交易: %s" % args.since)
        return 0

    enriched = []
    unmatched = 0
    for t in trades:
        truth = prior_session_truth(ledger, t["trade_date"])
        if truth is None:
            unmatched += 1
            continue
        row = dict(t)
        row["truth_vix"] = truth.get("vix_close")
        row["truth_date"] = truth.get("date")
        row["term_slope"] = truth.get("term_slope")
        row["truth_bucket"] = bucket_of(row["truth_vix"])
        row["proxy_bucket"] = bucket_of(t.get("entry_vix"))
        row["term_state"] = ("倒挂" if (row["term_slope"] or 0) > 0 else "正常") \
            if row["term_slope"] is not None else "(missing)"
        enriched.append(row)

    print("窗口 %s 起: %d 笔已平仓 / %d 个交易日 (无前置账本 %d 笔)"
          % (args.since, len(enriched), len({r['trade_date'] for r in enriched}), unmatched))

    by_truth = summarize(enriched, "truth_bucket")
    by_proxy = summarize(enriched, "proxy_bucket")
    by_term = summarize(enriched, "term_state")
    print_table("① 按【决策时可见的真值 VIX】(T-1 现货收盘) 分层", by_truth)
    print_table("② 按【代理 VIX】(entry_vix, 系统实际看到的) 分层", by_proxy)
    print_table("③ 按【期限结构】(T-1: VIX9D/VIX3M-1, >0 倒挂)", by_term)

    # 代理与真值分档不一致: 说明代理会把哪一档看错
    mism = [r for r in enriched if r["truth_bucket"] and r["proxy_bucket"]
            and r["truth_bucket"] != r["proxy_bucket"]]
    print("\n④ 代理分档 ≠ 真值分档: %d/%d 笔 (%.1f%%)"
          % (len(mism), len(enriched), len(mism) / len(enriched) * 100))
    for r in mism[:8]:
        print("   %s %s truth=%.2f(%s) proxy=%s(%s)"
              % (r["trade_date"], r["symbol"], r["truth_vix"], r["truth_bucket"],
                 r.get("entry_vix"), r["proxy_bucket"]))

    # 分级缩仓的粗算术 (费用不随规模等比 ⇒ 仅上界参考)
    print("\n⑤ 分级缩仓粗算术 (pnl_net × 倍数; 费用非等比 ⇒ 仅作上界参考)")
    for mult, lo in ((0.5, 28.0), (0.7, 25.0)):
        hit = [r for r in enriched if (r["truth_vix"] or 0) >= lo]
        raw = sum(float(r.get("pnl_net") or 0) for r in hit)
        scaled = raw * mult
        print("   truth>=%.0f 时 ×%.1f: 命中 %d 笔/%d 日, 原 net %.2f → 缩仓后 %.2f (差 %+.2f)"
              % (lo, mult, len(hit), len({r['trade_date'] for r in hit}), raw, scaled, scaled - raw))

    if args.json:
        Path(args.json).write_text(json.dumps({
            "since": args.since, "n": len(enriched), "unmatched": unmatched,
            "by_truth": by_truth, "by_proxy": by_proxy, "by_term": by_term,
            "mismatch": len(mism),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n已写入 %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())