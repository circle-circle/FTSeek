#!/usr/bin/env python3
"""C15 判据复现: 组合方向偏斜 guardrail 若恢复硬拦, 对被拦标的的历史结局是多少。

只读承诺: sqlite `mode=ro`, 不写库、不下单、不改任何文件。

人群/口径
---------
- 台账: `data/shadow/portfolio_execution_guardrail.jsonl`
  (2026-10-08 F1 摘闸后每笔执行前评估落一行, `enforced=False` = 只记不拦)。
- 受害者 = 台账行 `skip_ids` (硬拦时真会被杀的指令), **不是** 行内 `symbol`
  (后者是"当时在检查的那笔", 可能不是 victim)。
- ⚠️ `playbook_instructions.id` 是**每日内序号, 跨日复用** ⇒ 指令必须按
  `(date, id)` 复合键定位。用 `{id: row}` 会把 10-08 的受害者静默映射到 6-23 的
  成交行, 得出符号相反的结论 (+$109 vs 实际 −$354) 且算术自洽不报错。
- 成交按 `(symbol, trade_date)` 匹配, 取 `trade_records.pnl_net` (净口径)。

输出
----
全样本 Σ / 剔最大单笔 Σ / 中位 / 赢亏笔数 / 逐笔明细。
判据见 docs/ADJUDICATED-CRITERIA.md C15。
"""
import json
import sqlite3
import statistics as st
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "data" / "shadow" / "portfolio_execution_guardrail.jsonl"
DB = ROOT / "data" / "ftseek_us.db"


def main() -> int:
    rows = [json.loads(l) for l in LEDGER.open(encoding="utf-8") if l.strip()]
    skip_rows = [r for r in rows if r.get("decision") == "SKIP"]
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    inst = {(str(r["date"]), int(r["id"])): dict(r)
            for r in conn.execute("SELECT id, symbol, date FROM playbook_instructions")}
    trade = {(t["symbol"], t["trade_date"]): dict(t)
             for t in conn.execute(
                 "SELECT symbol, trade_date, direction, pnl_net, exit_reason "
                 "FROM trade_records WHERE exit_price > 0")}

    victims = set()
    for r in skip_rows:
        for sid in (r.get("skip_ids") or []):
            victims.add((str(r["date"]), int(sid)))

    hits, fills = [], 0
    for key in sorted(victims):
        row = inst.get(key)
        if not row:
            continue
        t = trade.get((row["symbol"], key[0]))
        if not t:
            continue
        hits.append(t)
    hits.sort(key=lambda t: t["pnl_net"])
    n = len(hits)
    total = sum(t["pnl_net"] for t in hits)

    print(f"台账行 {len(rows)} (SKIP {len(skip_rows)}, 全部 enforced=False)")
    print(f"去重受害者指令 {len(victims)} / 实际成交 {n} / 其余未成交(硬拦也无影响)")
    print(f"若硬拦: 少做 {n} 笔, Σ净额 = {total:+.2f}  (正=硬拦的代价, 负=硬拦的收益)")
    if n >= 2:
        print(f"  剔最大单笔({hits[0]['pnl_net']:+.2f})后 Σ = {total - hits[0]['pnl_net']:+.2f}")
        print(f"  均值 {st.mean(t['pnl_net'] for t in hits):+.2f} / 中位 {st.median(t['pnl_net'] for t in hits):+.2f}")
    print(f"  盈利 {sum(1 for t in hits if t['pnl_net'] > 0)} / 亏损 {sum(1 for t in hits if t['pnl_net'] <= 0)}")
    for t in hits:
        print(f"    {t['symbol']:9s}  {t['direction']:11s} {t['pnl_net']:+9.2f}  {t['exit_reason']}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())