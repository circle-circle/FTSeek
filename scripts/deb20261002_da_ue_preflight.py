"""溯源预检: DA/UE 深度分析字段对「入场即错」的可用性 (填充率 + join 完整性)

纪律 (memory): 列存在≠有数据 — PRAGMA + 填充率 + 采样验语义, 禁假设值自证。
纪律 (skill): 凡 JOIN playbook_instructions 必须 COUNT(DISTINCT t.id) 核行数, 防扇出虚增。

目标变量: 入场即错 = 已平仓 ∧ pnl_net 非空 ∧ COALESCE(mfe_pct,0) < 0.3
人群: entry_time >= 2026-06-27 (playbook 参数冻结时代), 排除首跑测试批 (A9)
只读: mode=ro, 零副作用。
"""
import sqlite3
from pathlib import Path

BASE = Path("/opt/ftseek/FTSeek-US")
c = sqlite3.connect("file:" + str(BASE / "data/ftseek_us.db") + "?mode=ro", uri=True)
c.row_factory = sqlite3.Row

POP = ("entry_time >= '2026-06-27' AND exit_time IS NOT NULL AND pnl_net IS NOT NULL "
       "AND COALESCE(json_extract(metadata_json,'$.exclusion_reason'),'')=''")
POP_T = ("t.entry_time >= '2026-06-27' AND t.exit_time IS NOT NULL AND t.pnl_net IS NOT NULL "
         "AND COALESCE(json_extract(t.metadata_json,'$.exclusion_reason'),'')=''")
J = ("FROM trade_records t JOIN playbook_instructions i ON i.decision_id = t.decision_id "
     "WHERE " + POP_T)
DEAD_T = "COALESCE(t.mfe_pct,0) < 0.3"
EMPTY = "('','0','0.0','None','null','{}','[]')"

n_pop = c.execute("SELECT COUNT(*) FROM trade_records WHERE " + POP).fetchone()[0]
n_dead = c.execute(
    "SELECT COUNT(*) FROM trade_records WHERE " + POP + " AND " + DEAD_T.replace("t.", "")
).fetchone()[0]
print("=== 人群 ===")
print("  n=" + str(n_pop) + "  死单=" + str(n_dead)
      + " (" + format(100.0 * n_dead / n_pop, ".1f") + "%)")

print("\n=== join 完整性: trade_records.decision_id -> playbook_instructions ===")
r = c.execute("SELECT COUNT(*) AS rows_joined, COUNT(DISTINCT t.id) AS distinct_trades " + J).fetchone()
fanout = "YES-BAD" if r["rows_joined"] != r["distinct_trades"] else "no"
print("  join行数=" + str(r["rows_joined"]) + "  distinct_trade=" + str(r["distinct_trades"])
      + "  扇出=" + fanout)
matched = r["distinct_trades"]
print("  可join覆盖率 = " + str(matched) + "/" + str(n_pop)
      + " = " + format(100.0 * matched / n_pop, ".1f") + "%")

print("\n=== decision_id 源前缀分布 (人群内, 死单率) ===")
q = ("SELECT CASE WHEN decision_id LIKE 'DA%' THEN 'DA'"
     " WHEN decision_id LIKE 'universe_expansion%' THEN 'UE-feed'"
     " WHEN decision_id LIKE 'UE%' THEN 'UE-direct'"
     " WHEN decision_id LIKE 'intraday_reentry%' THEN 'reentry'"
     " ELSE COALESCE(SUBSTR(decision_id,1,14),'(NULL)') END src,"
     " COUNT(*) n, SUM(CASE WHEN " + DEAD_T.replace("t.", "") + " THEN 1 ELSE 0 END) dead,"
     " ROUND(SUM(COALESCE(pnl_net,realized_pnl)),1) net"
     " FROM trade_records WHERE " + POP + " GROUP BY src ORDER BY n DESC")
for r in c.execute(q):
    print("  " + str(r["src"]).ljust(16) + " n=" + str(r["n"]).rjust(4)
          + " 死=" + str(r["dead"]).rjust(4)
          + " (" + format(100.0 * r["dead"] / r["n"], "5.1f") + "%)"
          + " 净=" + str(r["net"]).rjust(9))

print("\n=== trade_records 侧 DA/UE 分析字段填充率 (人群内) ===")
TR_FIELDS = ["signal_strength", "signal_confidence", "final_strength", "consensus_level",
             "voting_result", "model_scores", "f1_trend", "f2_momentum", "f3_volume",
             "f4_orderflow", "f5_lifecycle", "factor_detail", "entry_vix", "market_regime",
             "source_type", "entry_session", "signal_details", "decision_reason"]
for f in TR_FIELDS:
    row = c.execute("SELECT COUNT(*) tot, SUM(CASE WHEN " + f
                    + " IS NOT NULL AND TRIM(CAST(" + f + " AS TEXT)) NOT IN " + EMPTY
                    + " THEN 1 ELSE 0 END) filled FROM trade_records WHERE " + POP).fetchone()
    pct = 100.0 * (row["filled"] or 0) / row["tot"] if row["tot"] else 0.0
    flag = "OK " if pct >= 95 else ("MID" if pct >= 30 else "LOW")
    print("  [" + flag + "] " + f.ljust(22) + str(row["filled"] or 0).rjust(4)
          + "/" + str(row["tot"]).ljust(4) + " = " + format(pct, "5.1f") + "%")

print("\n=== playbook_instructions 侧 DA/UE 深度分析字段填充率 (join 人群) ===")
PI_FIELDS = ["ok_dimensions", "pattern_verdict", "confidence", "decision_conf",
             "horizon", "horizon_confidence", "vol_bucket", "atr_pct", "model_used",
             "market_regime", "sector", "data_quality_json", "conditions_json",
             "risks_json", "matched_patterns_json", "thesis", "invalidation",
             "mfe_analysis", "is_ab_test", "risk_reward"]
for f in PI_FIELDS:
    row = c.execute("SELECT COUNT(DISTINCT t.id) tot, COUNT(DISTINCT CASE WHEN i." + f
                    + " IS NOT NULL AND TRIM(CAST(i." + f + " AS TEXT)) NOT IN " + EMPTY
                    + " THEN t.id END) filled " + J).fetchone()
    pct = 100.0 * (row["filled"] or 0) / row["tot"] if row["tot"] else 0.0
    flag = "OK " if pct >= 95 else ("MID" if pct >= 30 else "LOW")
    print("  [" + flag + "] " + f.ljust(24) + str(row["filled"] or 0).rjust(4)
          + "/" + str(row["tot"]).ljust(4) + " = " + format(pct, "5.1f") + "%")

print("\n=== 语义采样: 真实取值分布 (禁假设值自证) ===")
for f in ("ok_dimensions", "pattern_verdict"):
    print("  --- " + f + " ---")
    for r in c.execute("SELECT i." + f + " v, COUNT(DISTINCT t.id) n " + J
                       + " GROUP BY v ORDER BY n DESC LIMIT 8"):
        print("      n=" + str(r["n"]).rjust(4) + "  " + str(r["v"])[:70])
for f in ("decision_conf", "confidence", "horizon_confidence"):
    row = c.execute("SELECT MIN(CAST(i." + f + " AS REAL)) mn, MAX(CAST(i." + f
                    + " AS REAL)) mx, AVG(CAST(i." + f + " AS REAL)) av,"
                    " COUNT(DISTINCT t.id) n " + J + " AND i." + f + " IS NOT NULL").fetchone()
    av = row["av"] if row["av"] is not None else 0.0
    print("  --- " + f + ": min=" + str(row["mn"]) + " max=" + str(row["mx"])
          + " avg=" + format(av, ".2f") + " n=" + str(row["n"]))
