#!/usr/bin/env python3
"""TAKE_PROFIT 死规则重校观测影子 (record-only, 零交易行为变更)
================================================================
预注册: docs/analysis/2026-09-24-take-profit-recalibration-preregistration.md
出处: C 类策略影子预注册批 (2026-09-24 用户批准)

语义: 对每笔已平仓交易, 从 trade_records.mfe_pct + 既有退出台账,
离线复算"若 TP 阈值为 X% 会在何时触发、相对实际退出的 PnL 差"。
纯只读观测, 不改任何生产参数/阈值/权重。

口径铁律: realized_pnl 是毛盈亏, 净额=pnl_net 优先否则 realized_pnl-fees_est。
本脚本用 pnl_net 作为实际净额, 假设 TP 触发时的净额为:
  entry_price × tp_threshold × quantity × direction_sign - fees_est

当前 TP 配置 (仅供参考, 本脚本不读取/导入 config):
  各 exit profile 阈值 4.5%-25%, 带 min_tp_rr=1.5 门。
  本脚本测试的候选阈值为纯静态栅格 [1%-5%], 与生产 TP 无耦合。

mfe_pct 口径: DB 存储为百分比数值 (如 2.2887 = 2.29%), 本脚本读取后除以 100
转换为小数 (0.0229) 再与候选阈值 (小数, 0.01 = 1%) 直接比较。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DB_DEFAULT = Path("/opt/ftseek/FTSeek-US/data/ftseek_us.db")
OUT_DEFAULT = Path("/opt/ftseek/FTSeek-US/data/shadow/take_profit_counterfactual_latest.json")

# 候选 TP 阈值 (小数: 0.01 = 1%)
TP_THRESHOLDS: List[float] = [0.01, 0.015, 0.02, 0.025, 0.03, 0.04, 0.05]


# ────────────────────────────────────────────────────────────
# 纯函数: 核心分析
# ────────────────────────────────────────────────────────────

def _direction_sign(direction: str) -> float:
    """LONG=+1, SHORT=-1; 其余返回 0 (调用方应跳过)."""
    d = (direction or "").upper()
    if d == "LONG":
        return 1.0
    if d == "SHORT":
        return -1.0
    return 0.0


def analyze_trades(
    trades: Sequence[Dict[str, Any]],
    thresholds: Sequence[float],
) -> Dict[str, Any]:
    """对每笔已平仓交易 × 每个 TP 阈值, 复算触发与 PnL 差。

    Args:
        trades: 每笔交易为一个 dict, 至少包含:
            trade_id, symbol, direction, entry_price, quantity,
            mfe_pct, pnl_net, fees_est
        thresholds: 候选 TP 阈值序列 (小数, 如 0.01 = 1%)

    Returns:
        {
          "generated_at": ISO timestamp,
          "n_trades": int,
          "thresholds": {
            "<threshold>": {
              "n_triggered": int,
              "n_improved": int,
              "n_degraded": int,
              "net_delta": float,
              "pct_improved": float,
              "details": [per-trade records, ...]
            }, ...
          }
        }
    """
    result: Dict[str, Any] = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "n_trades": len(trades),
        "thresholds": {},
    }

    for tp in thresholds:
        n_triggered = 0
        n_improved = 0
        n_degraded = 0
        net_delta = 0.0
        details: List[Dict[str, Any]] = []

        for tr in trades:
            mfe = float(tr.get("mfe_pct") or 0.0)
            if mfe < tp:
                continue  # 未触发

            n_triggered += 1
            sign = _direction_sign(tr.get("direction", ""))
            if sign == 0.0:
                continue

            entry_price = float(tr.get("entry_price") or 0.0)
            quantity = float(tr.get("quantity") or 0.0)
            fees_est = float(tr.get("fees_est") or 0.0)
            actual_pnl = float(tr.get("pnl_net") or 0.0)

            # TP hypothetical PnL
            hypo_pnl = entry_price * tp * quantity * sign - fees_est
            delta = hypo_pnl - actual_pnl

            if hypo_pnl > actual_pnl:
                n_improved += 1
            elif hypo_pnl < actual_pnl:
                n_degraded += 1
            # hypo_pnl == actual_pnl: 持平, 不计入 improved/degraded

            net_delta += delta
            details.append({
                "trade_id": tr.get("trade_id"),
                "symbol": tr.get("symbol"),
                "direction": tr.get("direction"),
                "mfe_pct": mfe,
                "entry_price": entry_price,
                "quantity": quantity,
                "fees_est": fees_est,
                "actual_pnl_net": actual_pnl,
                "hypo_pnl_net": round(hypo_pnl, 4),
                "delta": round(delta, 4),
            })

        pct_improved = (n_improved / n_triggered * 100.0) if n_triggered > 0 else 0.0

        result["thresholds"][str(tp)] = {
            "tp_threshold": tp,
            "n_triggered": n_triggered,
            "n_improved": n_improved,
            "n_degraded": n_degraded,
            "net_delta": round(net_delta, 4),
            "pct_improved": round(pct_improved, 2),
            "details": details,
        }

    return result


# ────────────────────────────────────────────────────────────
# DB 读取 (只读)
# ────────────────────────────────────────────────────────────

def load_closed_playbook_sim_trades(db_path: str) -> List[Dict[str, Any]]:
    """从 SQLite DB 只读加载所有已平仓的 PLAYBOOK SIM 交易。

    过滤条件:
      - trade_mode = 'simulate'
      - source_type LIKE 'PLAYBOOK%'
      - exit_time IS NOT NULL (已平仓)
      - pnl_net IS NOT NULL (有净额记录)
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, trade_date, symbol, direction, entry_time, exit_time,
                   entry_price, exit_price, quantity, realized_pnl, pnl_net,
                   fees_est, mfe_pct
            FROM trade_records
            WHERE trade_mode = 'simulate'
              AND source_type LIKE 'PLAYBOOK%'
              AND exit_time IS NOT NULL
              AND pnl_net IS NOT NULL
            ORDER BY id
            """
        ).fetchall()
    finally:
        conn.close()

    out: List[Dict[str, Any]] = []
    for r in rows:
        # DB mfe_pct 存储为百分比数值 (如 2.29 = 2.29%); 转换为小数 (0.0229)
        # 以便与候选阈值 (小数, 0.01 = 1%) 直接比较。
        raw_mfe = float(r["mfe_pct"] or 0.0)
        out.append({
            "trade_id": r["id"],
            "trade_date": r["trade_date"],
            "symbol": r["symbol"],
            "direction": (r["direction"] or "").upper(),
            "entry_price": float(r["entry_price"] or 0.0),
            "exit_price": float(r["exit_price"] or 0.0) if r["exit_price"] else None,
            "quantity": float(r["quantity"] or 0.0),
            "pnl_net": float(r["pnl_net"]),
            "fees_est": float(r["fees_est"] or 0.0),
            "mfe_pct": raw_mfe / 100.0,
        })
    return out


# ────────────────────────────────────────────────────────────
# 输出
# ────────────────────────────────────────────────────────────

def print_summary(report: Dict[str, Any]) -> None:
    """向 stdout 打印汇总表。"""
    n = report["n_trades"]
    print(f"═══ TAKE_PROFIT 死规则重校影子 (n={n} 笔已平仓) ═══")
    print(f"生成时间: {report['generated_at']}\n")
    print(f"{'TP%':>6s}  {'触发':>5s}  {'改善':>5s}  {'恶化':>5s}  "
          f"{'改善%':>7s}  {'净Δ($)':>10s}")
    print("─" * 58)

    for tp_str, data in report["thresholds"].items():
        tp = data["tp_threshold"]
        print(
            f"{tp * 100:5.1f}%  "
            f"{data['n_triggered']:5d}  "
            f"{data['n_improved']:5d}  "
            f"{data['n_degraded']:5d}  "
            f"{data['pct_improved']:6.1f}%  "
            f"{data['net_delta']:+10.2f}"
        )

    print("─" * 58)
    print("口径: hypo_pnl = entry_price × tp × qty × sign - fees_est")
    print("       Δ = hypo_pnl - actual_pnl_net")
    print("       改善: hypo > actual | 恶化: hypo < actual")


def write_json_report(report: Dict[str, Any], out_path: Path) -> None:
    """幂等覆盖写入 JSON 报告。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))


# ────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="TAKE_PROFIT counterfactual replay shadow (read-only)"
    )
    ap.add_argument(
        "--db", type=str, default=str(DB_DEFAULT),
        help=f"Path to SQLite DB (default: {DB_DEFAULT})"
    )
    ap.add_argument(
        "--out", type=str, default=str(OUT_DEFAULT),
        help=f"Output JSON report path (default: {OUT_DEFAULT})"
    )
    args = ap.parse_args()

    # PYTEST_CURRENT_TEST guard: 测试环境使用测试提供的 DB 路径
    if os.environ.get("PYTEST_CURRENT_TEST"):
        db_path = args.db
    else:
        db_path = args.db

    db_path_obj = Path(db_path)
    if not db_path_obj.exists():
        print(f"ERROR: DB not found: {db_path}", file=sys.stderr)
        return 1

    # 加载交易数据
    trades = load_closed_playbook_sim_trades(db_path)
    if not trades:
        print("WARNING: No closed PLAYBOOK SIM trades found.", file=sys.stderr)

    # 分析
    report = analyze_trades(trades, TP_THRESHOLDS)

    # 输出
    print_summary(report)

    out_path = Path(args.out)
    write_json_report(report, out_path)
    print(f"\n报告已写入: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
