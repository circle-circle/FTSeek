#!/usr/bin/env python3
"""F1 (DEB-20260804): condition 级期望反馈 — 生成 DA prompt 黑名单段

滚动 30 日窗 + 双窗口 (30d/60d) + 跨 regime 验证 + bootstrap 显著性,
产出 data/condition_expectancy_latest.json (含预格式化 prompt 段)。

入名单门槛 (DEB-20260804 F1 修订版):
  - 硬警示: 30d 窗 n≥8 且 mean<0 且 60d 窗同号 且 双 regime 同号负 且 bootstrap p(mean≥0)≤0.005
  - 软警示: 30d 窗 n≥8 且 mean<0 且 60d 窗同号 且 bootstrap p(mean≥0)≤0.03
  - 观察名单: 单 regime 翻正的条件 (不注入 prompt, 仅记录)

回滚: 删除本脚本 + crontab 行; DA 侧注入由 unified_config.yaml
playbook.condition_expectancy_prompt_enabled=false 关闭。
"""
import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "condition_expectancy_latest.json"
WINDOW_DAYS = 30
MIN_N = 8
BOOT_ITERS = 2000


def main():
    today = datetime.now()
    lo30 = (today - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")
    lo60 = (today - timedelta(days=WINDOW_DAYS * 2)).strftime("%Y-%m-%d")
    hi = today.strftime("%Y-%m-%d")

    # 重建 inst 索引 (含 symbol/date 供 fallback)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    inst = {}
    for r in con.execute(
        "SELECT id, date, symbol, conditions_json, market_regime FROM playbook_instructions"
    ):
        try:
            conds = json.loads(r["conditions_json"] or "[]")
            if not isinstance(conds, list):
                conds = []
        except json.JSONDecodeError:
            conds = []
        inst[r["id"]] = {
            "date": r["date"], "symbol": r["symbol"],
            "conds": {str(c) for c in conds},
            "regime": (r["market_regime"] or "UNKNOWN").upper(),
        }
    by_sd = defaultdict(list)
    for iid, v in inst.items():
        by_sd[(v["symbol"], v["date"])].append(iid)

    def collect(lo):
        pools = defaultdict(list)
        q = """SELECT trade_date, symbol, COALESCE(pnl_net, realized_pnl) AS pnl,
                      metadata_json
               FROM trade_records
               WHERE trade_date >= ? AND trade_date <= ? AND trade_mode='simulate'"""
        for r in con.execute(q, (lo, hi)):
            iid = None
            try:
                iid = json.loads(r["metadata_json"] or "{}").get("playbook_instruction_id")
            except json.JSONDecodeError:
                pass
            if iid not in inst:
                cands = by_sd.get((r["symbol"], r["trade_date"]), [])
                iid = cands[0] if len(cands) == 1 else None
            if not iid:
                continue
            v = inst[iid]
            for c in v["conds"]:
                pools[c].append((float(r["pnl"] or 0), v["regime"]))
        return pools

    p30 = collect(lo30)
    p60 = collect(lo60)
    con.close()

    rnd = random.Random(42)

    def boot_p(vals):
        """p(mean ≥ 0) 单侧 bootstrap"""
        n = len(vals)
        if n < 3:
            return 1.0
        cnt = 0
        for _ in range(BOOT_ITERS):
            if sum(rnd.choices(vals, k=n)) / n >= 0:
                cnt += 1
        return cnt / BOOT_ITERS

    hard, soft, watch = [], [], []
    for c, rows in sorted(p30.items()):
        if len(rows) < MIN_N:
            continue
        v30 = [p for p, _ in rows]
        mean30 = sum(v30) / len(v30)
        v60 = [p for p, _ in p60.get(c, [])]
        mean60 = sum(v60) / len(v60) if v60 else mean30
        if mean30 >= 0 or mean60 >= 0:
            continue  # 双窗口同号负才可入名单
        by_reg = defaultdict(list)
        for p, reg in rows:
            by_reg[reg].append(p)
        reg_means = {
            reg: sum(v) / len(v) for reg, v in by_reg.items() if len(v) >= 5
        }
        all_neg = bool(reg_means) and all(m < 0 for m in reg_means.values())
        p_val = boot_p(v30)
        entry = {
            "condition": c,
            "n30": len(rows),
            "mean30": round(mean30, 2),
            "n60": len(v60),
            "mean60": round(mean60, 2),
            "regime_means": {k: round(v, 2) for k, v in sorted(reg_means.items())},
            "boot_p_ge0": round(p_val, 4),
            "net30": round(sum(v30), 2),
        }
        if not all_neg:
            watch.append({**entry, "note": "regime 不同号, 仅观察"})
        elif p_val <= 0.005:
            hard.append(entry)
        elif p_val <= 0.03:
            soft.append(entry)
        else:
            watch.append(entry)
    hard.sort(key=lambda e: e["mean30"])
    soft.sort(key=lambda e: e["mean30"])

    lines = ["以下指令条件近30日统计上稳定亏损 (滚动30日窗, 双窗口+跨regime验证), 请避免或要求更强佐证:"]
    for e in hard:
        reg = "/".join(f"{k}{v:+.1f}" for k, v in e["regime_means"].items())
        lines.append(
            f"- 【硬警示】{e['condition']}: 近30日 {e['n30']}笔 平均{e['mean30']:+.2f}美元/笔 "
            f"(regime: {reg}, 显著性p={1 - e['boot_p_ge0']:.3f})"
        )
    for e in soft:
        lines.append(
            f"- 【软警示】{e['condition']}: 近30日 {e['n30']}笔 平均{e['mean30']:+.2f}美元/笔"
        )
    lines.append("注: 这是历史统计非绝对禁令; 若有独立强逻辑可保留, 但须在 thesis 中说明理由。")
    prompt_block = "\n".join(lines) if (hard or soft) else ""

    out = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "window_days": WINDOW_DAYS,
        "hard": hard,
        "soft": soft,
        "watch": watch,
        "prompt_block": prompt_block,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[condition_expectancy] hard={len(hard)} soft={len(soft)} watch={len(watch)}")
    for e in hard + soft:
        print(f"  {e['condition']}: n={e['n30']} {e['mean30']:+.2f}$/笔 p={e['boot_p_ge0']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
