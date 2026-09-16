# -*- coding: utf-8 -*-
"""全窗量化: DA planned_* (raw_instruction_json, 全量在库) vs 落库 entry/SL/TP.

维度:
  A. re-price 发生率与幅度
  B. RR (盈亏比) 漂移: DA planned RR vs 落库 RR
  C. SL 距离漂移 (机械 ATR 百分比 vs DA 结构位)
  D. TP 机械重算 (落库 TP = entry - sl_dist×mult?) 脱离 DA 结构位
  E. 分组结局: re-priced vs 未 re-price 的 pnl/exit_reason
"""
import json
import argparse
import sqlite3
import statistics as st

DEFAULT_SINCE = "2026-08-15"
_parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
_parser.add_argument("--since", default=DEFAULT_SINCE,
                     help="ISO 起始日期 (默认 %(default)s)")
_args = _parser.parse_args()

db = sqlite3.connect("file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row

rows = db.execute(
    """SELECT i.id, i.date, i.symbol, i.direction, i.entry_price, i.stop_loss,
              i.take_profit, i.decision_id, i.raw_instruction_json, i.executed,
              t.pnl_net, t.exit_reason, t.mfe_pct
       FROM playbook_instructions i
       LEFT JOIN trade_records t ON t.decision_id = i.decision_id
            AND t.source_type = 'PLAYBOOK_SIM'
       WHERE i.date >= ? AND i.direction IN ('BUY','SELL_SHORT')
         AND i.entry_price IS NOT NULL AND i.entry_price > 0""",
    (_args.since,),
).fetchall()

n_total = n_planned = n_repriced = 0
rr_da, rr_db = [], []
rr_deg = 0
sl_da_pct, sl_db_pct = [], []
repriced_pnl, plain_pnl = [], []
repriced_exit, plain_exit = {}, {}
samples = []
for r in rows:
    n_total += 1
    try:
        raw = json.loads(r["raw_instruction_json"] or "{}")
    except Exception:
        continue
    pe, ps, pt = raw.get("planned_entry"), raw.get("planned_stop_loss"), raw.get("planned_take_profit")
    if not all(isinstance(x, (int, float)) and x > 0 for x in (pe, ps, pt)):
        continue
    n_planned += 1
    db_e, db_s, db_t = r["entry_price"], r["stop_loss"], r["take_profit"]
    if not all(isinstance(x, (int, float)) and x > 0 for x in (db_e, db_s, db_t)):
        continue
    moved = abs(db_e / pe - 1) > 0.002
    if moved:
        n_repriced += 1
    short = r["direction"] == "SELL_SHORT"
    if short:
        risk_da, rew_da = ps - pe, pe - pt
        risk_db, rew_db = db_s - db_e, db_e - db_t
    else:
        risk_da, rew_da = pe - ps, pt - pe
        risk_db, rew_db = db_e - db_s, db_t - db_e
    if risk_da <= 0 or risk_db <= 0:
        continue
    a, b = rew_da / risk_da, rew_db / risk_db
    sl_da_pct.append(risk_da / pe * 100)
    sl_db_pct.append(risk_db / db_e * 100)
    if moved:
        rr_da.append(a); rr_db.append(b)
        if b < a - 0.05:
            rr_deg += 1
    # 结局分组 (只算已成交平仓)
    if r["pnl_net"] is not None:
        if moved:
            repriced_pnl.append(r["pnl_net"])
            repriced_exit[r["exit_reason"]] = repriced_exit.get(r["exit_reason"], 0) + 1
        else:
            plain_pnl.append(r["pnl_net"])
            plain_exit[r["exit_reason"]] = plain_exit.get(r["exit_reason"], 0) + 1
    if moved and len(samples) < 14:
        samples.append((r["date"][5:], r["symbol"].split(".")[-1], r["direction"][:4],
                        f"DA e{pe}/s{ps}/t{pt} RR={a:.2f} SL距={risk_da/pe*100:.1f}%",
                        f"→落库 e{db_e}/s{db_s}/t{db_t} RR={b:.2f} SL距={risk_db/db_e*100:.1f}%",
                        f"pnl={r['pnl_net']}", r["exit_reason"]))

print("=" * 78)
print(f"DA planned_* vs 落库 (re-price 后) — 全窗量化 ({_args.since} 起)")
print("=" * 78)
print(f"指令 {n_total} | 带完整 planned_entry/SL/TP {n_planned} | 被 re-price 移动 entry {n_repriced}"
      f" ({n_repriced/max(n_planned,1)*100:.0f}%)")
if rr_da:
    print(f"\nA/B. re-priced 组 RR: DA 中位 {st.median(rr_da):.2f} → 落库中位 {st.median(rr_db):.2f}"
          f" | RR 恶化>0.05: {rr_deg}/{len(rr_da)} = {rr_deg/len(rr_da)*100:.0f}%")
print(f"C. SL 距离: DA planned 中位 {st.median(sl_da_pct):.2f}% → 落库中位 {st.median(sl_db_pct):.2f}%"
      f" (re-price 用 vol_bucket ATR% 机械重算)")
print(f"\nE. 结局分组 (已平仓):")
if repriced_pnl:
    print(f"   re-priced 组: n={len(repriced_pnl)} 净{sum(repriced_pnl):+.0f} "
          f"均{st.mean(repriced_pnl):+.1f} WR={sum(1 for x in repriced_pnl if x>0)/len(repriced_pnl)*100:.0f}%")
    print(f"     exit 分布: {dict(sorted(repriced_exit.items(), key=lambda x:-x[1]))}")
if plain_pnl:
    print(f"   未re-price组: n={len(plain_pnl)} 净{sum(plain_pnl):+.0f} "
          f"均{st.mean(plain_pnl):+.1f} WR={sum(1 for x in plain_pnl if x>0)/len(plain_pnl)*100:.0f}%")
    print(f"     exit 分布: {dict(sorted(plain_exit.items(), key=lambda x:-x[1]))}")
print("\n样本明细 (re-priced):")
for s in samples:
    print("  ", " | ".join(str(x) for x in s))
