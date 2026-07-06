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
    from scripts.utils.market_guard import exit_if_not_trading_day
    exit_if_not_trading_day()
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
    
    # ── 3. 交易IC: 因子 → 实际交易 P&L (7/7 新增) ──
    # 与 IC(预测能力) 互补: trade_ic 衡量因子的实际交易盈利能力
    # 用 trade_records.realized_pnl_pct 代替 fwd_ret_t1
    # 用 trade_records.metadata_json.conditions 代替 factors_json
    print("\n=== 3. 交易IC (因子→实际P&L) ===")
    trade_rows = inst_db.execute("""
        SELECT direction, pnl_pct, realized_pnl,
               json_extract(metadata_json, '$.conditions') as conditions_str,
               exit_reason, holding_hours,
               json_extract(metadata_json, '$.peak_price') as peak_price,
               entry_price
        FROM trade_records
        WHERE source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
          AND exit_price IS NOT NULL AND exit_price > 0
          AND exit_reason IS NOT NULL AND exit_reason != ''
    """).fetchall()
    print(f"  闭环交易: {len(trade_rows)}条")

    # 按因子拆分: factor -> [(signed_pnl_pct, pnl, direction, exit_reason)]
    factor_trades = defaultdict(list)
    for r in trade_rows:
        conds_str = r["conditions_str"] or '[]'
        try:
            conds = json.loads(conds_str)
        except Exception:
            conds = []
        if not conds:
            continue
        pnl_pct = float(r["pnl_pct"] or 0)
        # signed: SHORT 的盈利为正, 亏损为负; 已经在 pnl_pct 里方向调整过了
        # 但 signed_strength 需要反映方向: LONG 用原始 pnl_pct, SHORT 反转
        signed = pnl_pct if r["direction"] == "LONG" else pnl_pct  # pnl_pct 已经是方向调整后的
        for c in conds:
            factor_trades[c].append((signed, pnl_pct, r["direction"], r["exit_reason"], float(r["realized_pnl"] or 0)))

    # MFE per trade
    def _mfe_pct(r):
        peak = r["peak_price"]
        entry = float(r["entry_price"] or 0)
        if not peak or entry <= 0:
            return 0.0
        peak = float(peak)
        if r["direction"] == "LONG":
            return (peak - entry) / entry * 100
        else:
            return (entry - peak) / entry * 100

    factor_trade_ic = {}
    print(f"\n  {'Factor':<30} {'N':>3} {'TradeIC':>8} {'WR%':>6} {'avgPnL%':>8} {'avgPnL$':>8} {'avgMFE%':>7} {'Verdict'}")
    print("  " + "-" * 90)
    for factor, trades in sorted(factor_trades.items(), key=lambda x: -len(x[1])):
        ic, n = calc_ic([(t[0], t[1], t[2]) for t in trades])
        if ic is None:
            print(f"  {factor:<30} {n:>3} {'N/A':>8} — 样本不足")
            continue
        wins = sum(1 for t in trades if t[1] > 0)
        avg_pnl_pct = sum(t[1] for t in trades) / n
        avg_pnl_usd = sum(t[4] for t in trades) / n
        # MFE: 需要重新查询 (trades 里没有存)
        # 从 trade_rows 重新匹配
        factor_mfes = []
        for r in trade_rows:
            conds_str = r["conditions_str"] or '[]'
            try:
                conds = json.loads(conds_str)
            except Exception:
                conds = []
            if factor in conds:
                factor_mfes.append(_mfe_pct(r))
        avg_mfe = sum(factor_mfes) / len(factor_mfes) if factor_mfes else 0
        verdict = "✅正alpha" if ic > 0.05 else ("❌负alpha" if ic < -0.05 else "⚪无alpha")
        print(f"  {factor:<30} {n:>3} {ic:>+8.4f} {wins/n*100:>5.1f}% {avg_pnl_pct:>+7.4f} {avg_pnl_usd:>+7.0f} {avg_mfe:>6.1f}% {verdict}")
        factor_trade_ic[factor] = {
            "trade_ic": ic,
            "n": n,
            "wr": wins / n,
            "avg_pnl_pct": avg_pnl_pct,
            "avg_pnl_usd": round(avg_pnl_usd, 2),
            "avg_mfe_pct": round(avg_mfe, 2),
        }

    # 4. 保存结果
    print("\n=== 4. 保存因子IC结果 ===")
    output_path = "data/factor_ic_latest.json"
    # 合并 IC 和 trade_ic 到同一个文件
    for factor, trade_data in factor_trade_ic.items():
        if factor in factor_ic_results:
            factor_ic_results[factor]["trade_ic"] = trade_data["trade_ic"]
            factor_ic_results[factor]["trade_n"] = trade_data["n"]
            factor_ic_results[factor]["trade_wr"] = trade_data["wr"]
            factor_ic_results[factor]["trade_avg_pnl_pct"] = trade_data["avg_pnl_pct"]
            factor_ic_results[factor]["trade_avg_pnl_usd"] = trade_data["avg_pnl_usd"]
            factor_ic_results[factor]["trade_avg_mfe_pct"] = trade_data["avg_mfe_pct"]
        else:
            factor_ic_results[factor] = {
                "ic": 0,
                "n": 0,
                "wr": trade_data["wr"],
                "avg_ret": 0,
                "trade_ic": trade_data["trade_ic"],
                "trade_n": trade_data["n"],
                "trade_wr": trade_data["wr"],
                "trade_avg_pnl_pct": trade_data["avg_pnl_pct"],
                "trade_avg_pnl_usd": trade_data["avg_pnl_usd"],
                "trade_avg_mfe_pct": trade_data["avg_mfe_pct"],
            }

    with open(output_path, "w") as f:
        json.dump(factor_ic_results, f, indent=2, default=str)
    print(f"  已保存到 {output_path}")

    # 5. 汇总
    print("\n=== 5. 汇总 ===")
    positive = {k: v for k, v in factor_ic_results.items() if v.get("ic", 0) > 0.05}
    negative = {k: v for k, v in factor_ic_results.items() if v.get("ic", 0) < -0.05}
    neutral = {k: v for k, v in factor_ic_results.items() if -0.05 <= v.get("ic", 0) <= 0.05}
    print(f"  ✅ 正alpha因子(IC>0.05): {len(positive)} — {list(positive.keys())}")
    print(f"  ❌ 负alpha因子(IC<-0.05): {len(negative)} — {list(negative.keys())}")
    print(f"  ⚪ 无alpha因子: {len(neutral)} — {list(neutral.keys())}")

    trade_positive = {k: v for k, v in factor_trade_ic.items() if v["trade_ic"] > 0.05}
    trade_negative = {k: v for k, v in factor_trade_ic.items() if v["trade_ic"] < -0.05}
    print(f"\n  ✅ 交易正alpha(TradeIC>0.05): {len(trade_positive)} — {list(trade_positive.keys())}")
    print(f"  ❌ 交易负alpha(TradeIC<-0.05): {len(trade_negative)} — {list(trade_negative.keys())}")

    # IC vs TradeIC 分歧
    print("\n  === IC vs TradeIC 分歧 ===")
    for factor in factor_ic_results:
        ic = factor_ic_results[factor].get("ic", 0)
        tic = factor_ic_results[factor].get("trade_ic")
        if tic is not None:
            agree = (ic > 0.05 and tic > 0.05) or (ic < -0.05 and tic < -0.05)
            if not agree and abs(ic) > 0.01:
                print(f"  ⚠️ {factor}: IC={ic:+.4f} vs TradeIC={tic:+.4f} — 分歧!")
    snap_db.close()
    inst_db.close()

if __name__ == "__main__":
    main()
