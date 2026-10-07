#!/usr/bin/env python3
"""因子负edge闸门 shadow 记分牌 (2026-08-21, record-only, 用户批准落地)。

背景 (盘后复盘 8/20 行动项P2):
  LONG 侧因子 pe_below_industry (n=228, 胜率24.6%, -$2,802) 与 news_positive
  (n=362, 胜率22.7%, -$2,085) 累计 verdict=negative, 但负edge闸门自 7/11 起停用
  (_FACTOR_BLACKLIST_ENABLED=False, 当时理由"v2下这些配对几乎不交易"已过期)。
  8/14-20 窗口 54 笔 LONG 中 48 笔引用这两个因子。

本脚本做什么 (只记不拦):
  对 playbook_instructions 里已 executed 的指令, 按闸门谓词 (single n>=30 或
  pair n>=8, verdict=negative) 判定"若闸门开启会被 SKIP", 与 trade_records 实际
  结局 join, 落 data/factor_neg_edge_shadow.jsonl。攒判别力样本:
  被标 SKIP 组净PnL 显著 < 放行组 → 闸门有判别力, 可考虑重开; 否则证伪。

判分 (门记分牌教训: 阈值先验方向对称+判别力):
  - 每周日复盘汇报: 被标组 n / 净PnL / 胜率 vs 放行组, |Δ胜率|>=10pp 且被标组
    净PnL<0 记为判别力证据。
  - n>=30 才下结论; 不足只观察。

用法: PYTHONPATH=. venv/bin/python scripts/factor_neg_edge_shadow.py
挂载: 盘后综合复盘 Step (trade_records 当日收盘后齐全)。幂等: (symbol,date) 去重。
回滚: 删 jsonl + 从 cron prompt 移除步骤。
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
OUTPUT = ROOT / "data" / "factor_neg_edge_shadow.jsonl"

# 闸门谓词阈值 (与 pipeline 停用前逻辑同源: single n>=30 / pair n>=8, verdict=negative)
SINGLE_MIN_N = 30
PAIR_MIN_N = 8


def _read_all_rows():
    """读全历史 jsonl 行 (供分方向汇总); 损坏行跳过。"""
    if not OUTPUT.exists():
        return []
    out = []
    for line in OUTPUT.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _seen_keys():
    if not OUTPUT.exists():
        return set()
    seen = set()
    for line in OUTPUT.read_text().splitlines():
        try:
            r = json.loads(line)
            seen.add((r.get("symbol"), r.get("trade_date")))
        except Exception:
            continue
    return seen


def main():
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 负edge因子集合 (single + pair)
    neg_singles = {r["factor_key"] for r in cur.execute(
        "SELECT factor_key FROM playbook_factor_stats WHERE analysis_level='single' "
        "AND verdict='negative' AND total_trades>=?", (SINGLE_MIN_N,))}
    neg_pairs = {frozenset(r["factor_key"].split("|")) for r in cur.execute(
        "SELECT factor_key FROM playbook_factor_stats WHERE analysis_level='pair' "
        "AND verdict='negative' AND total_trades>=?", (PAIR_MIN_N,))}

    # 已执行指令 (triggered=1 且有成交结局) — 只评估真实发生过的交易, 不评估纸上SKIP
    rows = cur.execute("""
        SELECT i.date, i.symbol, i.direction, i.conditions_json, i.decision_id,
               t.pnl_net, t.exit_reason, t.entry_time
        FROM playbook_instructions i
        JOIN trade_records t ON t.symbol = i.symbol AND t.decision_id = i.decision_id
        WHERE i.executed = 1 AND t.exit_reason IS NOT NULL AND t.exit_reason != ''
    """).fetchall()
    conn.close()

    seen = _seen_keys()
    rows_out, n_new = [], 0
    for r in rows:
        key = (r["symbol"], r["date"])
        if key in seen:
            continue
        try:
            conds = json.loads(r["conditions_json"] or "[]")
        except Exception:
            conds = []
        hit_singles = [c for c in conds if c in neg_singles]
        hit_pairs = sorted({tuple(sorted((c1, c2))) for i, c1 in enumerate(conds)
                            for c2 in conds[i+1:]
                            if frozenset((c1, c2)) in neg_pairs})
        # 谓词: 命中任一 negative single, 或命中任一 negative pair → 闸门会 SKIP
        would_skip = bool(hit_singles or hit_pairs)
        rows_out.append({
            "trade_date": r["date"],
            "symbol": r["symbol"],
            "direction": r["direction"],
            "decision_id": r["decision_id"],
            "conditions": conds,
            "hit_negative_singles": hit_singles,
            "hit_negative_pairs": [list(p) for p in hit_pairs],
            "would_skip": would_skip,
            "actual_pnl_net": round(r["pnl_net"] or 0.0, 2),
            "actual_exit_reason": r["exit_reason"],
            "recorded_at": datetime.utcnow().isoformat() + "Z",
        })
        seen.add(key)
        n_new += 1

    if rows_out:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTPUT, "a", encoding="utf-8") as f:
            for row in rows_out:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 汇总 (供复盘报告引用)
    skip_pnl = sum(r["actual_pnl_net"] for r in rows_out if r["would_skip"])
    pass_pnl = sum(r["actual_pnl_net"] for r in rows_out if not r["would_skip"])
    n_skip = sum(1 for r in rows_out if r["would_skip"])
    print(f"[factor_neg_edge_shadow] {n_new} 条新记录 "
          f"(被标SKIP {n_skip} 笔 净{skip_pnl:+.1f} / 放行 {len(rows_out)-n_skip} 笔 净{pass_pnl:+.1f})")

    # ── 全历史分方向判别力读数 (2026-10-06 修: Step 9 明令「必须分 LONG/SHORT」, 但脚本只印
    #    当日聚合 ⇒ 报告 agent 被迫自行解析 jsonl 并误读字段名 (把 actual_pnl_net 当 pnl_net,
    #    报「596条全null 分方向不可用」—— 实测 596/596 非空)。让脚本自己印全量分方向读数,
    #    消除误读面。判据: 被标组净PnL 显著 < 放行组 = 门有判别力 (负edge因子该拦)。
    _all = _read_all_rows()
    if _all:
        from collections import defaultdict
        _agg = defaultdict(lambda: {"n": 0, "pnl": 0.0, "win": 0})
        for r in _all:
            d = r.get("direction") or "?"
            arm = "SKIP" if r.get("would_skip") else "PASS"
            k = (d, arm)
            pnl = r.get("actual_pnl_net")
            if pnl is None:
                continue
            _agg[k]["n"] += 1
            _agg[k]["pnl"] += pnl
            _agg[k]["win"] += 1 if pnl > 0 else 0
        print(f"[factor_neg_edge_shadow] 全历史分方向 (n={len(_all)} 行, 字段=actual_pnl_net):")
        for d in ("BUY", "SELL_SHORT"):
            s = _agg.get((d, "SKIP"), {"n": 0, "pnl": 0.0, "win": 0})
            pa = _agg.get((d, "PASS"), {"n": 0, "pnl": 0.0, "win": 0})
            s_wr = (s["win"] / s["n"] * 100) if s["n"] else 0.0
            p_wr = (pa["win"] / pa["n"] * 100) if pa["n"] else 0.0
            s_avg = (s["pnl"] / s["n"]) if s["n"] else 0.0
            p_avg = (pa["pnl"] / pa["n"]) if pa["n"] else 0.0
            # 判别力: 被标组均PnL < 放行组均PnL ⇒ 门筛掉了更差的单 (有判别力)
            disc = "有判别力" if (s["n"] and pa["n"] and s_avg < p_avg) else "无判别力/反向"
            print(f"  {d:10s} 被标SKIP n={s['n']:4d} 净{s['pnl']:+9.2f} 均{s_avg:+7.2f} WR={s_wr:.0f}% | "
                  f"放行 n={pa['n']:4d} 净{pa['pnl']:+9.2f} 均{p_avg:+7.2f} WR={p_wr:.0f}% ⇒ {disc}")


if __name__ == "__main__":
    sys.exit(main())
