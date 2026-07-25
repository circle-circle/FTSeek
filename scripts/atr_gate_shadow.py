#!/usr/bin/env python3
"""ATR 门变体干跑判分 (2026-07-25, 首夜复盘议题①, 用户批准) — 只读, 零 live 影响。

背景 (双重确认):
  - 07-24 首夜: 赢家 (NVDA/TSM/BABA/GOOG, atr 3.5-3.9%) vs 输家 (RTX/MRK/LMT/TMO,
    atr 2.58-3.00%) 按 ATR 完美二分
  - 近14天分桶: <2.5% 35%WR -$6.7/笔 | 2.5-3.0% 52%WR -$8.5/笔 (现行门放行的次差桶)
    | 3.0-4.0% 71%WR +$3.2/笔 (唯一正期望桶) | >=4.0% 38%WR -$25/笔

方法论 (可测性铁律): 所有候选门都比现行 low_atr 2.5% 更严 → "候选拦但实际成交"
  的单 PnL 已知, counterfactual_saved = -realized_pnl 完全可测 (拦对省亏为正,
  拦错杀赢为负)。候选比现行松的方向 (无) 路径不可观测, 不做。

用法:
  ./venv/bin/python scripts/atr_gate_shadow.py                # 昨日一天
  ./venv/bin/python scripts/atr_gate_shadow.py --since 2026-07-11   # 区间累计判分
落盘: data/shadow/atr_gate_shadow_latest.json (+ 逐日 jsonl 追加)
裁决口径 (2 周样本后): 变体 saved_total 显著为正 且 kill_winner 占比低 → 提 DEB 实单化。
"""
import argparse
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
OUT_DIR = ROOT / "data" / "shadow"
ET = ZoneInfo("America/New_York")

# 候选门变体 (lo=下限 atr_pct<lo 拦 / hi=上限 atr_pct>=hi 拦; 现行 live=lo 0.025)
VARIANTS = {
    "live_lo2.5":      {"lo": 0.025, "hi": None},   # 对照: 现行门 (定义上 0 拦截, 校验用)
    "lo2.8":           {"lo": 0.028, "hi": None},
    "lo3.0":           {"lo": 0.030, "hi": None},
    "hi4.0":           {"lo": 0.025, "hi": 0.040},
    "lo3.0_hi4.0":     {"lo": 0.030, "hi": 0.040},
    "lo3.0_hi5.0":     {"lo": 0.030, "hi": 0.050},
}


def get_atr(row) -> float:
    """指令表 atr_pct 首选, metadata.atr 兜底 (仅 <0.5 时可信为 pct)."""
    a = row["atr_pct"]
    if a is None:
        try:
            m = json.loads(row["metadata_json"] or "{}")
            a = m.get("atr")
            if a is not None and float(a) > 0.5:
                a = None
        except Exception:
            a = None
    return float(a) if a is not None else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="起始 trade_date (含), 缺省=最近一个交易日")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    if args.since:
        since = args.since
    else:
        r = conn.execute(
            "SELECT MAX(trade_date) d FROM trade_records WHERE source_type LIKE 'PLAYBOOK%' "
            "AND exit_time IS NOT NULL AND exit_time!=''").fetchone()
        since = r["d"]
    rows = conn.execute(
        "SELECT t.trade_date, t.symbol, t.realized_pnl, t.mfe_pct, t.metadata_json, i.atr_pct "
        "FROM trade_records t LEFT JOIN playbook_instructions i "
        "  ON t.symbol=i.symbol AND t.trade_date=i.date "
        "WHERE t.trade_date>=? AND t.source_type LIKE 'PLAYBOOK%' "
        "AND t.exit_time IS NOT NULL AND t.exit_time!='' AND t.realized_pnl IS NOT NULL",
        (since,)).fetchall()
    conn.close()

    print(f"═══ ATR 门变体干跑判分: trade_date >= {since} (n={len(rows)}) ═══")
    print("口径: 候选门拦截「实际已成交」单 → saved=-pnl (正=拦对省亏, 负=杀赢家)\n")

    result = {"since": since, "generated_at": datetime.now(ET).isoformat(),
              "n_trades": len(rows), "variants": {}}
    for name, v in VARIANTS.items():
        blocked, saved, killed_winners, na = [], 0.0, [], 0
        for r in rows:
            a = get_atr(r)
            if a is None:
                na += 1
                continue
            hit = (a < v["lo"]) or (v["hi"] is not None and a >= v["hi"])
            if hit:
                pnl = float(r["realized_pnl"])
                blocked.append({"symbol": r["symbol"], "date": r["trade_date"],
                                "atr_pct": round(a, 4), "pnl": round(pnl, 2)})
                saved -= pnl
                if pnl > 0:
                    killed_winners.append(f"{r['symbol']}(+{pnl:.0f})")
        result["variants"][name] = {
            "blocked_n": len(blocked), "saved_total": round(saved, 2),
            "killed_winners": killed_winners, "atr_na": na, "blocked": blocked,
        }
        kw = f" 杀赢家: {', '.join(killed_winners)}" if killed_winners else ""
        print(f"{name:14s} 拦 {len(blocked):3d} 笔  省亏 ${saved:+9.2f}{kw}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    latest = OUT_DIR / "atr_gate_shadow_latest.json"
    latest.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    with open(OUT_DIR / "atr_gate_shadow.jsonl", "a", encoding="utf-8") as f:
        slim = {k: (v if k != "variants" else {n: {kk: vv for kk, vv in d.items() if kk != "blocked"}
                                              for n, d in v.items()}) for k, v in result.items()}
        f.write(json.dumps(slim, ensure_ascii=False) + "\n")
    print(f"\n落盘: {latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
