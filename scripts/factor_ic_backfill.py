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
        SELECT direction, strength, fwd_ret_t1, alpha_t1, factors_json, strategy_id
        FROM signal_snapshots
        WHERE fwd_ret_t1 IS NOT NULL AND strength IS NOT NULL
        AND factors_json IS NOT NULL AND factors_json != 'null'
    """).fetchall()
    print(f"  有factors+fwd_ret的记录: {len(rows)}")
    
    # 归一化: factors_json 有 3 种历史形态, 统一拍平成因子名字符串列表
    #   - list[str]                     : 旧版 27-condition, 原样
    #   - list[dict]                    : deep_analysis 结构化 claim 列表, 取 claim_type
    #   - dict{claims,conditions,...}   : deep_analysis 完整输出, 取 conditions(str)+claims 的 claim_type
    #   - dict{condition: bool}         : 条件映射, 取值为真的键
    # 2026-07-10: 修复原 `for f in factors` 在 dict 元素上崩溃(unhashable), 且让 claim_type 进入 factor IC
    def _extract_factors(raw):
        out = []
        if isinstance(raw, list):
            for f in raw:
                if isinstance(f, str):
                    out.append(f)
                elif isinstance(f, dict):
                    ct = f.get("claim_type") or f.get("source_type")
                    if ct:
                        out.append(ct)
        elif isinstance(raw, dict):
            if "claims" in raw or "conditions" in raw:
                for c in (raw.get("conditions") or []):
                    if isinstance(c, str):
                        out.append(c)
                for cl in (raw.get("claims") or []):
                    if isinstance(cl, dict):
                        ct = cl.get("claim_type") or cl.get("source_type")
                        if ct:
                            out.append(ct)
            else:
                for k, v in raw.items():
                    if v and isinstance(k, str):
                        out.append(k)
        return list(dict.fromkeys(out))  # 每条记录内去重(因子命中与否, 不重复计数)

    # 按因子拆分：每条信号可能命中多个因子
    # 2026-07-10 修复: fwd_ret_t1 已是方向调整过的 (LONG=+raw, SHORT=-raw),
    #   原 code 再对 strength 做 signed 翻转 → 双重方向调整 → SHORT 信号的 IC 被反转.
    #   且 Pearson 对极端值敏感 (1 个 RIVN SHORT -8% 信号把 valuation_cheap IC 从 -0.03 拉到 +0.79).
    #   修法: 不再翻转 strength, 直接用 raw strength 与 fwd_ret_t1 求相关.
    #   同时取 alpha_t1 (已剥离大盘 beta) 用于 avg_alpha 盈利指标 (方案A).
    factor_signals = defaultdict(list)  # factor -> [(strength, fwd_ret, direction, alpha_t1)]
    for r in rows:
        raw = json.loads(r["factors_json"]) if r["factors_json"] else []
        if r["direction"] not in ("LONG", "SHORT"):
            continue  # NEUTRAL 无方向, 无法评估预测力
        strength = r["strength"]  # raw, 不翻转 — fwd_ret_t1 已方向调整
        for f in _extract_factors(raw):
            factor_signals[f].append((strength, r["fwd_ret_t1"], r["direction"],
                                      r["alpha_t1"] if r["alpha_t1"] is not None else r["fwd_ret_t1"]))
    
    def calc_ic(pairs):
        # 2026-07-10: Pearson → Spearman rank correlation
        #   Pearson 对极端值敏感: 1 个 RIVN SHORT -8% 信号把 valuation_cheap IC 从 -0.03 拉到 +0.79.
        #   Spearman 用 rank 消除极端值影响, 与 Pipeline A (claim_ic) 的方法一致.
        n = len(pairs)
        if n < 3:
            return None, n
        ss = [p[0] for p in pairs]
        rs = [p[1] for p in pairs]
        # rank
        def _rank(values):
            indexed = sorted(range(len(values)), key=lambda i: values[i])
            ranks = [0.0] * len(values)
            i = 0
            while i < len(indexed):
                j = i
                while j + 1 < len(indexed) and values[indexed[j + 1]] == values[indexed[i]]:
                    j += 1
                avg_rank = (i + 1 + j + 1) / 2.0
                for k in range(i, j + 1):
                    ranks[indexed[k]] = avg_rank
                i = j + 1
            return ranks
        xr = _rank(ss)
        yr = _rank(rs)
        mx = sum(xr) / n
        my = sum(yr) / n
        num = sum((a - mx) * (b - my) for a, b, *_ in zip(xr, yr))
        vx = sum((a - mx) ** 2 for a in xr)
        vy = sum((b - my) ** 2 for b in yr)
        if vx == 0 or vy == 0:
            return 0.0, n
        return (num / math.sqrt(vx * vy)), n
    
    # ── 2b. intraday horizon (F3, 2026-08-04): strength × signal→当日收盘 ──
    # 数据源勘误自 DEB-20260804: playbook_instructions.fwd_return_intraday (entry 锚定)
    #   + fwd_return_intraday_open (open 锚定, 诚实尺子 — entry 锚定含未成交限价纸面优势).
    #   两列均已方向调整 (ic_tracker.py: sign=1 BUY / -1 SHORT). 回滚: 删除本段+输出列.
    intraday_map = {}
    for r in inst_db.execute(
        "SELECT decision_id, fwd_return_intraday, fwd_return_intraday_open "
        "FROM playbook_instructions WHERE fwd_return_intraday IS NOT NULL"
    ).fetchall():
        intraday_map[r["decision_id"]] = (r["fwd_return_intraday"], r["fwd_return_intraday_open"])

    factor_signals_intraday = defaultdict(list)       # factor -> [(strength, ret_entry_anchor)]
    factor_signals_intraday_open = defaultdict(list)  # factor -> [(strength, ret_open_anchor)]
    n_intra = 0
    for r in snap_db.execute(
        "SELECT decision_id, direction, strength, factors_json FROM signal_snapshots "
        "WHERE strength IS NOT NULL AND factors_json IS NOT NULL AND factors_json != 'null'"
    ).fetchall():
        if r["direction"] not in ("LONG", "SHORT"):
            continue
        pair = intraday_map.get(r["decision_id"])
        if not pair:
            continue
        ret_entry, ret_open = pair
        n_intra += 1
        for f in _extract_factors(json.loads(r["factors_json"]) if r["factors_json"] else []):
            if ret_entry is not None:
                factor_signals_intraday[f].append((r["strength"], ret_entry))
            if ret_open is not None:
                factor_signals_intraday_open[f].append((r["strength"], ret_open))
    print(f"  intraday配对记录 (有signal→close回填): {n_intra}条")

    print(f"\n  {'Factor':<30} {'N':>5} {'IC':>8} {'WR%':>6} {'avgRet':>8} {'avgAlpha':>8} {'Verdict'}")
    print("  " + "-" * 85)
    
    factor_ic_results = {}
    for factor, pairs in sorted(factor_signals.items(), key=lambda x: -len(x[1])):
        ic, n = calc_ic(pairs)
        if ic is None:
            print(f"  {factor:<30} {n:>5} {'N/A':>8} — 样本不足")
            continue
        # WR: fwd_ret > 0 = 方向调整后盈利 (LONG: stock up, SHORT: stock down)
        wins = sum(1 for p in pairs if p[1] > 0)
        mr = sum(p[1] for p in pairs) / n
        # avg_alpha: 剥离大盘 beta 后的超额收益 (alpha_t1), fwd_ret 无 alpha 时用 fwd_ret 降级
        avg_alpha = sum(p[3] for p in pairs) / n
        verdict = "✅正alpha" if ic > 0.05 else ("❌负alpha" if ic < -0.05 else "⚪无alpha")
        print(f"  {factor:<30} {n:>5} {ic:>+8.4f} {wins/n*100:>5.1f}% {mr:>+8.4f} {avg_alpha:>+8.4f} {verdict}")
        factor_ic_results[factor] = {"ic": ic, "n": n, "wr": wins/n, "avg_ret": mr, "avg_alpha": avg_alpha}

    # F3: intraday IC 表 (entry 锚定 vs open 锚定并列; open 为诚实尺子)
    print(f"\n  === intraday IC (strength × signal→当日收盘, Spearman) ===")
    print(f"  {'Factor':<30} {'N':>5} {'IC(entry)':>10} {'IC(open)':>10}")
    print("  " + "-" * 60)
    _all_intra_factors = set(factor_signals_intraday) | set(factor_signals_intraday_open)
    for factor in sorted(_all_intra_factors, key=lambda f: -len(factor_signals_intraday.get(f, []))):
        pairs_e = factor_signals_intraday.get(factor, [])
        pairs_o = factor_signals_intraday_open.get(factor, [])
        ic_e, n_e = calc_ic(pairs_e) if len(pairs_e) >= 3 else (None, len(pairs_e))
        ic_o, n_o = calc_ic(pairs_o) if len(pairs_o) >= 3 else (None, len(pairs_o))
        se = f"{ic_e:+.4f}" if ic_e is not None else "N/A"
        so = f"{ic_o:+.4f}" if ic_o is not None else "N/A"
        print(f"  {factor:<30} {max(n_e, n_o):>5} {se:>10} {so:>10}")
        entry = factor_ic_results.setdefault(
            factor, {"ic": 0, "n": 0, "wr": 0, "avg_ret": 0, "avg_alpha": 0})
        entry["ic_intraday_entry"] = ic_e
        entry["n_intraday_entry"] = n_e
        entry["ic_intraday_open"] = ic_o
        entry["n_intraday_open"] = n_o

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

    # 按因子拆分: factor -> [(signed_pnl_pct, pnl, direction, exit_reason, realized_pnl, mfe_pct)]
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
        # signed: pnl_pct 已经是方向调整后的, 直接复用
        signed = pnl_pct
        mfe_pct = _mfe_pct(r)
        for c in conds:
            factor_trades[c].append((signed, pnl_pct, r["direction"], r["exit_reason"], float(r["realized_pnl"] or 0), mfe_pct))

    factor_trade_ic = {}
    # 2026-07-10 修复: 原 trade_ic = corr(pnl_pct, pnl_pct) 恒等于 ±1
    #   (signed 被直接设为 pnl_pct, calc_ic 又拿 pnl_pct 与自身求相关 → 自相关无意义).
    #   且无任何决策逻辑消费 trade_ic. 改用真实盈利度量: 胜率 + 平均 P&L, verdict 由 avgPnL% 定.
    print(f"\n  {'Factor':<30} {'N':>3} {'WR%':>6} {'avgPnL%':>8} {'avgPnL$':>8} {'avgMFE%':>7} {'Verdict'}")
    print("  " + "-" * 82)
    for factor, trades in sorted(factor_trades.items(), key=lambda x: -len(x[1])):
        n = len(trades)
        if n < 3:
            print(f"  {factor:<30} {n:>3}  — 样本不足")
            continue
        wins = sum(1 for t in trades if t[1] > 0)
        avg_pnl_pct = sum(t[1] for t in trades) / n
        avg_pnl_usd = sum(t[4] for t in trades) / n
        factor_mfes = [t[5] for t in trades]
        avg_mfe = sum(factor_mfes) / len(factor_mfes) if factor_mfes else 0
        verdict = "✅盈利" if avg_pnl_pct > 0.05 else ("❌亏损" if avg_pnl_pct < -0.05 else "⚪打平")
        print(f"  {factor:<30} {n:>3} {wins/n*100:>5.1f}% {avg_pnl_pct:>+7.4f} {avg_pnl_usd:>+7.0f} {avg_mfe:>6.1f}% {verdict}")
        factor_trade_ic[factor] = {
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
                "avg_alpha": 0,
                "trade_n": trade_data["n"],
                "trade_wr": trade_data["wr"],
                "trade_avg_pnl_pct": trade_data["avg_pnl_pct"],
                "trade_avg_pnl_usd": trade_data["avg_pnl_usd"],
                "trade_avg_mfe_pct": trade_data["avg_mfe_pct"],
            }

    # 2026-08-03 (改进批 任务#3): 内嵌生成时间戳 — 此前只能靠文件 mtime 判断新鲜度,
    #   周末 cron 不跑时无法区分"计划内停跑" vs "失败". 消费端按因子名取值或 n>=30 守卫,
    #   "_meta" 无 ic/n 字段被天然跳过, 零副作用.
    from datetime import datetime as _dt_meta, timezone as _tz_meta
    factor_ic_results["_meta"] = {"generated_at": _dt_meta.now(_tz_meta.utc).isoformat()}
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

    trade_positive = {k: v for k, v in factor_trade_ic.items() if v["avg_pnl_pct"] > 0.05}
    trade_negative = {k: v for k, v in factor_trade_ic.items() if v["avg_pnl_pct"] < -0.05}
    print(f"\n  ✅ 交易盈利(avgPnL%>0.05): {len(trade_positive)} — {list(trade_positive.keys())}")
    print(f"  ❌ 交易亏损(avgPnL%<-0.05): {len(trade_negative)} — {list(trade_negative.keys())}")

    # 预测IC vs 实际交易盈亏 分歧 (IC 说有 alpha 但真实交易反向 = 预测未兑现, 有意义的诊断)
    print("\n  === 预测IC vs 实际交易盈亏 分歧 ===")
    for factor in factor_ic_results:
        ic = factor_ic_results[factor].get("ic", 0)
        pnl = factor_ic_results[factor].get("trade_avg_pnl_pct")
        if pnl is not None and abs(ic) > 0.05:
            agree = (ic > 0.05 and pnl > 0) or (ic < -0.05 and pnl < 0)
            if not agree:
                print(f"  ⚠️ {factor}: 预测IC={ic:+.4f} 但实际avgPnL%={pnl:+.4f} — 预测未兑现!")
    snap_db.close()
    inst_db.close()

if __name__ == "__main__":
    main()
