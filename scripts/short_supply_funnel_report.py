#!/opt/ftseek/FTSeek-US/venv/bin/python3
"""
SHORT Supply Funnel Report — SHORT 供给漏斗影子观测 (record-only)

立项 (2026-08-20, DEB-20260820-architecture-quant-dual-audit P0-4, 白名单·攒样本):
  干净窗口实测: SHORT 仅占成交 12.8% (33/257), 且 DA 通道几乎不产 SHORT
  (DEB-20260819 §51: DA prompt 无对称 SHORT 触发规则)。
  在动任何机制 (如 prompt 对称化) 之前, 先用纯观测把漏斗逐日固化:
  **SHORT 到底在哪一层被饿死 — 生成层 / 触发层 / 执行层?**

口径 (与 exit_scorecard.py 对齐):
  - playbook_instructions: 计数 SSOT (不取 pnl — 见 exit_scorecard 幽灵单污染警告;
    outcome 分类计数安全)
  - $ 维度: trade_records (source_type 白名单) 按 decision_id 反查 realized_pnl/pnl_net
  - 通道判定 (decision_id 前缀):
      DA-*                   → DA        (Daily Analyst 指令)
      universe_expansion-*   → UE        (宇宙扩展通道)
      intraday_reentry-*     → REENTRY   (日内重入)
      TFB-*                  → TFB       (topup bridge, 8/6 已停)
      空/其他                 → GEN       (generate 主生成)
  - 方向: direction BUY / SELL_SHORT; SKIP 单 (skip_reason 非空) 单独计数

用法:
  python scripts/short_supply_funnel_report.py                 # 默认 since 2026-07-11
  python scripts/short_supply_funnel_report.py --since 2026-08-01
输出:
  data/short_supply_funnel_latest.json (+ _history.jsonl 逐日追加, 攒时间序列)
零行为影响: 纯只读 (DB 以 mode=ro 打开), 不触任何交易路径。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = "/opt/ftseek/FTSeek-US"
sys.path.insert(0, PROJECT_ROOT)

DB_PATH = f"{PROJECT_ROOT}/data/ftseek_us.db"
OUT_LATEST = f"{PROJECT_ROOT}/data/short_supply_funnel_latest.json"
OUT_HISTORY = f"{PROJECT_ROOT}/data/short_supply_funnel_history.jsonl"

PLAYBOOK_SOURCE_TYPES = ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE', 'PLAYBOOK_SHADOW', 'MANUAL_PLAYBOOK')

CHANNELS = ("DA", "UE", "GEN", "REENTRY", "TFB")
DIRECTIONS = ("BUY", "SELL_SHORT")

# trade_records 方向词汇 → instructions 词汇
TR_DIRECTION_MAP = {"LONG": "BUY", "SHORT": "SELL_SHORT", "BUY": "BUY", "SELL_SHORT": "SELL_SHORT"}


def _channel(decision_id: str | None) -> str:
    d = (decision_id or "").strip()
    if d.startswith("DA-"):
        return "DA"
    if d.startswith("universe_expansion"):
        return "UE"
    if d.startswith("intraday_reentry"):
        return "REENTRY"
    if d.startswith("TFB-"):
        return "TFB"
    return "GEN"


def main():
    ap = argparse.ArgumentParser(description="SHORT 供给漏斗影子观测 (record-only)")
    ap.add_argument("--since", default="2026-07-11", help="窗口起始 date")
    args = ap.parse_args()

    uri = Path(DB_PATH).as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT id, date, symbol, direction, decision_id, triggered, executed,
                  skip_reason, outcome
           FROM playbook_instructions
           WHERE date >= ?""",
        (args.since,),
    ).fetchall()

    # $ 维度: trade_records 已平仓单, 按 decision_id 归并
    ph = ",".join("?" * len(PLAYBOOK_SOURCE_TYPES))
    trs = conn.execute(
        f"""SELECT decision_id, direction, realized_pnl, pnl_net
            FROM trade_records
            WHERE source_type IN ({ph})
              AND trade_date >= ?
              AND exit_time IS NOT NULL AND exit_time != ''""",
        (*PLAYBOOK_SOURCE_TYPES, args.since),
    ).fetchall()
    conn.close()

    # ---- 漏斗计数 ----
    funnel = {
        ch: {
            d: {"instructions": 0, "skipped": 0, "triggered": 0, "executed": 0,
                "win": 0, "loss": 0, "other_outcome": 0}
            for d in DIRECTIONS
        }
        for ch in CHANNELS
    }
    for r in rows:
        ch = _channel(r["decision_id"])
        d = (r["direction"] or "").upper()
        if d not in DIRECTIONS:
            continue
        cell = funnel[ch][d]
        cell["instructions"] += 1
        if (r["skip_reason"] or "").strip():
            cell["skipped"] += 1
        if r["triggered"]:
            cell["triggered"] += 1
        if r["executed"]:
            cell["executed"] += 1
        oc = (r["outcome"] or "").lower()
        if oc == "win":
            cell["win"] += 1
        elif oc == "loss":
            cell["loss"] += 1
        elif oc:
            cell["other_outcome"] += 1

    # ---- $ 维度 ----
    pnl = {ch: {d: {"n": 0, "gross": 0.0, "net": 0.0} for d in DIRECTIONS}
           for ch in CHANNELS}
    both_pnl_null = 0
    for t in trs:
        ch = _channel(t["decision_id"])
        d = TR_DIRECTION_MAP.get((t["direction"] or "").upper())
        if d not in DIRECTIONS:
            continue
        cell = pnl[ch][d]
        cell["n"] += 1
        cell["gross"] += float(t["realized_pnl"] or 0.0)
        # 2026-10-15 修复: pnl_net 与 realized_pnl 双 NULL 时原写法 float(None) 必崩
        # (自 2026-09-20 起每跑必崩、静默 25 天 — error_alert 只认 "] ERROR:" 前缀)。
        net_val = t["pnl_net"] if t["pnl_net"] is not None else t["realized_pnl"]
        if net_val is None:
            both_pnl_null += 1  # 双 NULL 行按 0 计但必须透出 (数据质量问题不静默)
        cell["net"] += float(net_val or 0.0)

    # ---- 汇总 + SHORT 占比 (漏斗每一层) ----
    layer_totals = {}
    for layer in ("instructions", "skipped", "triggered", "executed", "win", "loss"):
        tot = sum(funnel[ch][d][layer] for ch in CHANNELS for d in DIRECTIONS)
        short = sum(funnel[ch]["SELL_SHORT"][layer] for ch in CHANNELS)
        layer_totals[layer] = {
            "total": tot,
            "short": short,
            "short_share_pct": round(100.0 * short / tot, 1) if tot else None,
        }
    n_tr = sum(pnl[ch][d]["n"] for ch in CHANNELS for d in DIRECTIONS)
    n_tr_short = sum(pnl[ch]["SELL_SHORT"]["n"] for ch in CHANNELS)

    rep = {
        "window_since": args.since,
        "layer_totals": layer_totals,
        "trades_closed": {
            "total": n_tr,
            "short": n_tr_short,
            "short_share_pct": round(100.0 * n_tr_short / n_tr, 1) if n_tr else None,
            "both_pnl_null": both_pnl_null,
        },
        "by_channel": {},
        "interpretation": (
            "观察 SHORT 在 instructions→triggered→executed 各层的占比衰减点: "
            "若生成层占比就低 → 供给问题在 prompt/筛选; 若生成层尚可但 triggered/executed "
            "骤降 → 门禁/执行层不对称。n<30 的层不下结论 (charter 七律)。"
        ),
    }
    for ch in CHANNELS:
        rep["by_channel"][ch] = {
            "funnel": funnel[ch],
            "pnl_closed": {
                d: {"n": pnl[ch][d]["n"],
                    "gross": round(pnl[ch][d]["gross"], 2),
                    "net": round(pnl[ch][d]["net"], 2)}
                for d in DIRECTIONS
            },
        }

    Path(OUT_LATEST).parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_LATEST, "w") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    with open(OUT_HISTORY, "a") as f:
        f.write(json.dumps(rep, ensure_ascii=False) + "\n")

    # ---- 人类可读 ----
    print("=" * 78)
    print(f"[short_supply_funnel] 窗口 since={args.since}")
    print("  漏斗各层 SHORT 占比:")
    for layer, v in layer_totals.items():
        share = f"{v['short_share_pct']}%" if v["short_share_pct"] is not None else "-"
        print(f"    {layer:>12}: SHORT {v['short']:>4} / {v['total']:<5} ({share})")
    print(f"    closed_trades: SHORT {n_tr_short:>4} / {n_tr:<5} "
          f"({rep['trades_closed']['short_share_pct']}%)")
    print("-" * 78)
    print("  分通道 (instructions → triggered → executed | win/loss | 净$):")
    for ch in CHANNELS:
        for d in DIRECTIONS:
            c = funnel[ch][d]
            p = pnl[ch][d]
            if c["instructions"] == 0 and p["n"] == 0:
                continue
            print(f"    {ch:>7} {d:>10}: {c['instructions']:>3} → {c['triggered']:>3} → "
                  f"{c['executed']:>3} | W{c['win']}/L{c['loss']} "
                  f"(skip {c['skipped']}) | n={p['n']} 净 ${p['net']:,.2f}")
    print(f"  → {OUT_LATEST}")
    print("=" * 78)


if __name__ == "__main__":
    main()
