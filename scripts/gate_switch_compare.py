#!/usr/bin/env python3
"""闸门切换效果对比 (debate-conf 实际 vs LLM自评 影子)。

用途: signal_gate 已从 LLM 自评 confidence 切到 debate-conf 分级 (2026-07-18)。
本脚本统计新口径 (实际 decision) 与旧口径 (shadow_llm_self_decision) 的分歧,
并关联 playbook_instructions 的真实成交盈亏, 量化切换收益。

只统计含新字段 (decision_conf) 的记录 — 即 2026-07-18 之后跑出的 gate 决策。

用法:
    ./venv/bin/python scripts/gate_switch_compare.py            # 全部新记录
    ./venv/bin/python scripts/gate_switch_compare.py --since 2026-07-18
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JSONL = os.path.join(_ROOT, "data", "da_gate_decisions.jsonl")
FTSEEK_DB = os.path.join(_ROOT, "data", "ftseek_us.db")


def _load_records(since: str):
    recs = []
    if not os.path.exists(JSONL):
        return recs
    for line in open(JSONL, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        # 只要含新字段的记录 (切换后)
        if r.get("decision_conf") is None:
            continue
        if since and (r.get("trade_date", "") < since):
            continue
        recs.append(r)
    return recs


def _outcomes():
    """按 (date, symbol_no_prefix) -> 成交结果。"""
    if not os.path.exists(FTSEEK_DB):
        return {}
    c = sqlite3.connect(FTSEEK_DB)
    c.row_factory = sqlite3.Row
    out = {}
    for r in c.execute("""
        SELECT date, symbol, executed, outcome, pnl
        FROM playbook_instructions
        WHERE executed=1 AND outcome IN ('win','loss')
    """):
        sym = str(r["symbol"]).replace("US.", "").upper()
        out[(r["date"], sym)] = {"outcome": r["outcome"], "pnl": r["pnl"] or 0}
    c.close()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="", help="仅统计该日期(含)之后, 如 2026-07-18")
    args = ap.parse_args()

    recs = _load_records(args.since)
    if not recs:
        print("暂无含新字段(decision_conf)的 gate 记录。")
        print("→ 切换于 2026-07-18 生效, 需等次日盘前 DA 跑过后才有数据。")
        return 0

    outc = _outcomes()

    # 分歧分类
    agree = 0
    new_pass_old_block = []   # 新放行 / 旧拦截 (切换新增的信号)
    new_block_old_pass = []   # 新拦截 / 旧放行 (切换新拦的信号)

    def _is_pass(d):
        return d in ("PASS", "WARN")  # WARN 也执行(小仓)

    for r in recs:
        new_d = r.get("decision", "")
        old_d = r.get("shadow_llm_self_decision", "")
        if _is_pass(new_d) == _is_pass(old_d):
            agree += 1
            continue
        key = (r.get("trade_date", ""), str(r.get("symbol", "")).replace("US.", "").upper())
        res = outc.get(key)
        item = {
            "date": r.get("trade_date", ""),
            "symbol": r.get("symbol", ""),
            "dir": r.get("direction", ""),
            "new": new_d, "old": old_d,
            "dconf": r.get("decision_conf"),
            "lconf": r.get("llm_self_conf"),
            "outcome": res["outcome"] if res else "-未成交/未平",
            "pnl": res["pnl"] if res else None,
        }
        if _is_pass(new_d) and not _is_pass(old_d):
            new_pass_old_block.append(item)
        else:
            new_block_old_pass.append(item)

    print("=" * 66)
    print(f"  闸门切换对比 (debate-conf 实际 vs LLM自评 影子)")
    print(f"  样本: {len(recs)} 条 gate 决策" + (f" (>= {args.since})" if args.since else ""))
    print("=" * 66)
    print(f"  两口径一致: {agree} 条")
    print(f"  新放行/旧拦截 (切换新增): {len(new_pass_old_block)} 条")
    print(f"  新拦截/旧放行 (切换新拦): {len(new_block_old_pass)} 条")

    def _dump(title, items):
        if not items:
            return
        print(f"\n── {title} ──")
        realized = [i for i in items if i["pnl"] is not None]
        for i in items:
            pnl = f"${i['pnl']:+.0f}" if i["pnl"] is not None else "  -"
            print(f"  {i['date']} {i['symbol']:9}{i['dir']:11} "
                  f"new={i['new']:5} old={i['old']:5} "
                  f"dconf={i['dconf']} lconf={i['lconf']} | {i['outcome']:6} {pnl}")
        if realized:
            tot = sum(i["pnl"] for i in realized)
            wins = sum(1 for i in realized if i["pnl"] > 0)
            print(f"  小计(已平仓 {len(realized)}笔): 净${tot:+.0f} 胜率{wins/len(realized):.0%}")

    _dump("切换新增放行的信号 (旧逻辑会误杀)", new_pass_old_block)
    _dump("切换新拦截的信号 (旧逻辑会放行)", new_block_old_pass)

    # 净效果
    gain = [i for i in new_pass_old_block if i["pnl"] is not None]
    avoid = [i for i in new_block_old_pass if i["pnl"] is not None]
    print("\n" + "=" * 66)
    print("  切换净效果 (仅已平仓样本):")
    if gain:
        print(f"    新增放行贡献: 净${sum(i['pnl'] for i in gain):+.0f} ({len(gain)}笔)")
    if avoid:
        print(f"    新拦截避免的盈亏: 净${sum(i['pnl'] for i in avoid):+.0f} ({len(avoid)}笔) "
              f"[若为负=切换正确避损; 若为正=切换误拦]")
    if not gain and not avoid:
        print("    暂无已平仓的分歧样本, 继续积累。")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
