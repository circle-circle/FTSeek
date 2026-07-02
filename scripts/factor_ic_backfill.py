#!/usr/bin/env python3
"""
因子级IC回填脚本

从 playbook_instructions.conditions_json 回填因子到 signal_snapshots.factors_json
然后计算每个因子的IC

Crontab: 35 5 * * 2-6 (在backfill_forward_returns后运行)
"""
import sys, os, sqlite3, json, math
from collections import defaultdict

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)
os.chdir(PROJECT_DIR)

from dotenv import load_dotenv
load_dotenv(".env.us")

def main():
    # 1. 回填factors_json到signal_snapshots（通过decision_id关联）
    print("=== 1. 回填factors_json到signal_snapshots ===")
    snap_db = sqlite3.connect("data/signal_snapshots.db")
    snap_db.row_factory = sqlite3.Row
    
    inst_db = sqlite3.connect("data/ftseek_us.db")
    inst_db.row_factory = sqlite3.Row
    
    # 找signal_snapshots中factors_json为NULL但有decision_id的记录
    pending = snap_db.execute("""
        SELECT id, decision_id FROM signal_snapshots
        WHERE factors_json IS NULL AND decision_id IS NOT NULL AND decision_id != ''
    """).fetchall()
    print(f"  待回填: {len(pending)}条")
    
    updated = 0
    for row in pending:
        # 从playbook_instructions查conditions_json
        inst = inst_db.execute(
            "SELECT conditions_json FROM playbook_instructions WHERE decision_id = ?",
            (row["decision_id"],)
        ).fetchone()
        if inst and inst["conditions_json"]:
            try:
                conditions = json.loads(inst["conditions_json"])
                if isinstance(conditions, list) and conditions:
                    snap_db.execute(
                        "UPDATE signal_snapshots SET factors_json = ? WHERE id = ?",
                        (json.dumps(conditions), row["id"])
                    )
                    updated += 1
            except Exception:
                pass
    
    snap_db.commit()
    print(f"  已回填: {updated}/{len(pending)}条")
    
    # 2. 计算因子级IC
    print("\n=== 2. 因子级IC计算 ===")
    rows = snap_db.execute("""
        SELECT direction, strength, fwd_ret_t1, factors_json, strategy_id
        FROM signal_snapshots
        WHERE fwd_ret_t1 IS NOT NULL AND strength IS NOT NULL
        AND factors_json IS NOT NULL AND factors_json != 'null'
    """).fetchall()
    print(f"  有factors+fwd_ret的记录: {len(rows)}")
    
    # 按因子拆分：每条信号可能命中多个因子
    factor_signals = defaultdict(list)  # factor -> [(signed_strength, fwd_ret, direction)]
    for r in rows:
        factors = json.loads(r["factors_json"]) if r["factors_json"] else []
        signed = r["strength"] if r["direction"] == "LONG" else (
            -r["strength"] if r["direction"] == "SHORT" else None
        )
        if signed is None:
            continue
        for f in factors:
            factor_signals[f].append((signed, r["fwd_ret_t1"], r["direction"]))
    
    def calc_ic(pairs):
        n = len(pairs)
        if n < 3:
            return None, n
        ss = [p[0] for p in pairs]
        rs = [p[1] for p in pairs]
        ms = sum(ss) / n
        mr = sum(rs) / n
        num = sum((s - ms) * (r - mr) for s, r, _ in pairs)
        ds = math.sqrt(sum((s - ms) ** 2 for s in ss))
        dr = math.sqrt(sum((r - mr) ** 2 for r in rs))
        return (num / (ds * dr) if ds * dr else 0), n
    
    print(f"\n  {'Factor':<30} {'N':>5} {'IC':>8} {'WR%':>6} {'avgRet':>8} {'Verdict'}")
    print("  " + "-" * 75)
    
    factor_ic_results = {}
    for factor, pairs in sorted(factor_signals.items(), key=lambda x: -len(x[1])):
        ic, n = calc_ic(pairs)
        if ic is None:
            print(f"  {factor:<30} {n:>5} {'N/A':>8} — 样本不足")
            continue
        wins = sum(1 for p in pairs if (p[2] == "LONG" and p[1] > 0) or (p[2] == "SHORT" and p[1] < 0))
        mr = sum(p[1] for p in pairs) / n
        verdict = "✅正alpha" if ic > 0.05 else ("❌负alpha" if ic < -0.05 else "⚪无alpha")
        print(f"  {factor:<30} {n:>5} {ic:>+8.4f} {wins/n*100:>5.1f}% {mr:>+8.4f} {verdict}")
        factor_ic_results[factor] = {"ic": ic, "n": n, "wr": wins/n, "avg_ret": mr}
    
    # 3. 保存结果
    print("\n=== 3. 保存因子IC结果 ===")
    output_path = "data/factor_ic_latest.json"
    with open(output_path, "w") as f:
        json.dump(factor_ic_results, f, indent=2, default=str)
    print(f"  已保存到 {output_path}")
    
    # 4. 汇总
    print("\n=== 汇总 ===")
    positive = {k: v for k, v in factor_ic_results.items() if v["ic"] > 0.05}
    negative = {k: v for k, v in factor_ic_results.items() if v["ic"] < -0.05}
    neutral = {k: v for k, v in factor_ic_results.items() if -0.05 <= v["ic"] <= 0.05}
    print(f"  ✅ 正alpha因子: {len(positive)} — {list(positive.keys())}")
    print(f"  ❌ 负alpha因子: {len(negative)} — {list(negative.keys())}")
    print(f"  ⚪ 无alpha因子: {len(neutral)} — {list(neutral.keys())}")
    
    snap_db.close()
    inst_db.close()

if __name__ == "__main__":
    main()
