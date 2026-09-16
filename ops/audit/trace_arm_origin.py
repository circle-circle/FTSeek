# -*- coding: utf-8 -*-
"""ARM 9/15 指令源头回溯: DA 原始输出 vs 落库指令 (re-price 后) 的风险几何对比."""
import json
import sqlite3

d = json.load(open("/opt/ftseek/FTSeek-US/data/da_checkpoint/2026-09-15/US.ARM.json"))
lo = d["_llm_output"]
ep = lo["execution_plan"]
print("=== DA 原始计划 (LLM 输出) ===")
print("entry", ep["entry_price"], "SL", ep["stop_loss"], "TP", ep["take_profit"],
      "chase_cap", ep["chase_cap_price"])
print("key_levels:", json.dumps(lo["key_levels"], ensure_ascii=False))
print("invalidation_triggers:", json.dumps(ep["invalidation_triggers"], ensure_ascii=False))
print("entry_strategy:", ep["entry_strategy"])

db = sqlite3.connect("file:/opt/ftseek/FTSeek-US/data/ftseek_us.db?mode=ro", uri=True)
db.row_factory = sqlite3.Row
r = db.execute("SELECT entry_price,stop_loss,take_profit,confidence,atr_pct,vol_bucket,"
               "raw_instruction_json FROM playbook_instructions WHERE id=2697").fetchone()
print("\n=== 落库指令 (re-price 改写后) ===")
print({k: r[k] for k in ("entry_price", "stop_loss", "take_profit", "confidence",
                         "atr_pct", "vol_bucket")})
raw = json.loads(r["raw_instruction_json"])
print("落库 chase_cap_price:", raw.get("chase_cap_price"))
print("落库 confirmation:", json.dumps(raw.get("confirmation"), ensure_ascii=False))

da_e, da_s, da_t = ep["entry_price"], ep["stop_loss"], ep["take_profit"]
db_e, db_s, db_t = r["entry_price"], r["stop_loss"], r["take_profit"]
print("\n=== 风险几何对比 (SHORT) ===")
print(f"DA  : 风险距离 {(da_s/da_e-1)*100:.2f}%  RR={(da_e-da_t)/(da_s-da_e):.2f}  "
      f"TP距 {(1-da_t/da_e)*100:.2f}%")
print(f"落库: 风险距离 {(db_s/db_e-1)*100:.2f}%  RR={(db_e-db_t)/(db_s-db_e):.2f}  "
      f"TP距 {(1-db_t/db_e)*100:.2f}%")
print(f"ATR% = {r['atr_pct']*100:.2f}%   (风险距离/ATR = DA {(da_s/da_e-1)*100/(r['atr_pct']*100):.2f}x"
      f" → 落库 {(db_s/db_e-1)*100/(r['atr_pct']*100):.2f}x)")

print("\n=== 结构位是否保住 ===")
inv = [t["value"] for t in ep["invalidation_triggers"] if t["metric"] == "收盘价"]
print(f"DA 失效线(收盘>){inv} | DA SL={da_s} | 落库 SL={db_s}")
if inv:
    ok_da = da_s > inv[0]
    ok_db = db_s > inv[0]
    print(f"  DA SL 在失效线{'之上✓' if ok_da else '之下✗'}; "
          f"落库 SL 在失效线{'之上✓' if ok_db else '之下✗ ← 论点未失效就先被止损'}")
sup = lo["key_levels"]["support"]
print(f"DA TP={da_t} | 落库 TP={db_t} | 结构支撑位={sup}")
print(f"  落库 TP 是否落在结构支撑位: {any(abs(db_t-s)/s < 0.01 for s in sup)}"
      f" (偏差: " + ", ".join(f"{s}:{(db_t/s-1)*100:+.1f}%" for s in sup) + ")")

print("\n=== 挑战者独立性 ===")
deb = lo.get("_debate", {})
print(f"challenger_model={deb.get('challenger_model')} fallback={deb.get('challenger_fallback')}")
print(f"debate_outcome={lo.get('debate_outcome')}")
print(f"score={lo.get('score')} confidence_semantic={lo.get('confidence_semantic')}")

print("\n=== DA 自己声明的数据缺口 ===")
print("data_quality_note:", str(lo.get("data_quality_note"))[:400])
print("evidence_gaps:", json.dumps(lo.get("evidence_gaps"), ensure_ascii=False)[:400])
print("\n_snapshot 里 DA 看到了什么价:")
snap = lo.get("_snapshot") or d.get("snapshot") or {}
if isinstance(snap, dict):
    for k in ("price", "last_price", "pre_price", "change_percent", "volume_ratio", "timestamp"):
        if k in snap:
            print(f"  {k} = {snap[k]}")
print("\ncheckpoint timestamp:", d.get("timestamp"), "| _llm_output ts:", lo.get("_timestamp"))
