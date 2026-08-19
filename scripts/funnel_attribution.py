# -*- coding: utf-8 -*-
"""分环节期望分解 — DEB-20260819 §十九-P0-1 (打磨的度量衡).

背景: A2 证明 DA 提名零边缘后, "边缘到底在哪一环"无人回答。本脚本把每笔闭环
交易按漏斗环节归因, 回答"每一分期望产自哪一环":

环节分解 (pnl_pct 沿漏斗路径分段, 方向校正后统一为"对的方向为正"):
  选股+方向  = entry→exit 全程 (有 mae 的票可再拆)
  入场时点   = 方向对的票中 mae 段 (入场后先逆行的代价)
  出场       = 方向对的票中 mfe−pnl 段 (峰值到出场没锁住的部分)
  方向       = 方向错票的亏损合计
输出: 各环节对总期望的贡献% + 按 strategy_id/regime 分层 + conf 校准。

用法: python scripts/funnel_attribution.py [--since N天] [--push]
数据: trade_records 闭环交易; mfe/mae 填充率 60%/37% (旧期缺, 缺失票只入全程段)。
"""
import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
DB = ROOT / "data" / "ftseek_us.db"


def load_trades(since_days):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    q = """SELECT trade_date, symbol, direction, entry_price, exit_price,
           quantity, pnl_pct, pnl_net, mfe_pct, mae_pct, signal_confidence,
           strategy_id, market_regime, exit_reason, decision_id
           FROM trade_records
           WHERE exit_time IS NOT NULL AND exit_time != '' AND pnl_pct IS NOT NULL"""
    if since_days:
        cutoff = (datetime.now() - timedelta(days=since_days)).strftime("%Y-%m-%d")
        q += f" AND trade_date >= '{cutoff}'"
    rows = con.execute(q).fetchall()
    con.close()
    return rows


def dir_sign(direction):
    return 1.0 if direction in ("LONG", "BUY") else -1.0  # noqa: 保留备用


def attribute(rows):
    """双口径归因 (DEB §二十 L1教训: 口径必须明确, 防混读).

    口径A (加性主分解): 总盈亏 = 方向正确合计 + 方向错误合计
    口径B (方向正确票的路径观测, 非加性):
        峰值浮盈 Σ(notional×mfe%) → 实际锁袋 Σpnl_net → 出场漏损 = 差
        入场后最大逆行 Σ(notional×|mae|%) = 拿住的成本/入场时点质量
    """
    total_pnl = 0.0
    win_pnl = loss_pnl = 0.0
    win_n = loss_n = 0
    peak_profit = 0.0      # 口径B: 方向正确票的峰值浮盈合计
    peak_n = 0
    mae_cost = 0.0         # 口径B: 入场后最大逆行合计
    mae_n = 0
    strat = defaultdict(lambda: {"pnl": 0.0, "n": 0, "win": 0})
    conf_buckets = defaultdict(lambda: {"pnl": 0.0, "n": 0, "win": 0})

    for r in rows:
        pnl_net = r["pnl_net"] or 0.0
        pnl_pct = (r["pnl_pct"] or 0.0) * 100  # 存的是小数
        mfe = r["mfe_pct"]                      # 已是百分比 (0.42=0.42%)
        mae = r["mae_pct"]
        total_pnl += pnl_net
        st = strat[r["strategy_id"] or "?"]
        st["pnl"] += pnl_net
        st["n"] += 1
        st["win"] += 1 if pnl_net > 0 else 0

        notional = (r["entry_price"] or 0) * (r["quantity"] or 0)
        if pnl_pct > 0:
            win_pnl += pnl_net
            win_n += 1
            # 口径B: 峰值浮盈与出场漏损
            if mfe is not None and mfe > pnl_pct and notional > 0:
                peak_profit += notional * mfe / 100
                peak_n += 1
            # 口径B: 入场后最大逆行 (DB 中 mae_pct 为正数表示不利幅度%)
            if mae is not None and mae > 0 and notional > 0:
                mae_cost += notional * mae / 100
                mae_n += 1
        else:
            loss_pnl += pnl_net
            loss_n += 1

        conf = r["signal_confidence"]
        if conf is not None and conf > 0:
            # conf ∈ (0,1]: 按 0.2 步长分桶, round 防截断
            b = min(1.0, round(conf * 5) / 5)
            conf_buckets[b]["pnl"] += pnl_net
            conf_buckets[b]["n"] += 1
            conf_buckets[b]["win"] += 1 if pnl_net > 0 else 0

    return {"total": total_pnl, "win_pnl": win_pnl, "win_n": win_n,
            "loss_pnl": loss_pnl, "loss_n": loss_n,
            "peak_profit": peak_profit, "peak_n": peak_n,
            "mae_cost": mae_cost, "mae_n": mae_n,
            "strat": dict(strat), "conf": dict(conf_buckets)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=int, default=0, help="近N天 (0=全部)")
    ap.add_argument("--json-out", default="")
    a = ap.parse_args()

    rows = load_trades(a.since or None)
    if not rows:
        print("[skip] 无闭环交易")
        return
    d = attribute(rows)
    total = d["total"]

    lines = [f"📊 分环节期望分解 ({len(rows)}笔闭环"
             + (f", 近{a.since}天" if a.since else ", 全期") + f")",
             f"总净盈亏: ${total:+.2f}", "",
             "── 口径A 加性主分解 (方向选择价值) ──",
             f"方向正确: ${d['win_pnl']:+.2f} (n={d['win_n']})",
             f"方向错误: ${d['loss_pnl']:+.2f} (n={d['loss_n']})"]
    if d["win_n"]:
        lines.append(f"单笔均盈亏: 赢${d['win_pnl']/d['win_n']:+.2f} / "
                     f"亏${d['loss_pnl']/d['loss_n']:+.2f}" if d["loss_n"] else "")
    lines += ["", "── 口径B 路径观测 (仅方向正确票, 非加性) ──",
              f"峰值浮盈: ${d['peak_profit']:+.2f} (n={d['peak_n']})",
              f"出场漏损(峰值−锁袋): ${d['peak_profit']-d['win_pnl']:+.2f}"
              if d["peak_n"] else "峰值浮盈: 数据不足",
              f"入场后最大逆行(拿住成本): -${d['mae_cost']:.2f} (n={d['mae_n']})",
              "", "── 按策略源分层 ──"]
    for k, v in sorted(d["strat"].items(), key=lambda x: -x[1]["pnl"]):
        wr = v["win"] / v["n"] * 100 if v["n"] else 0
        lines.append(f"{k}: ${v['pnl']:+.2f} (n={v['n']} 胜率{wr:.0f}%)")
    lines += ["", "── conf 校准 (置信度→实际表现) ──"]
    for b in sorted(d["conf"]):
        v = d["conf"][b]
        wr = v["win"] / v["n"] * 100 if v["n"] else 0
        lines.append(f"conf≈{b:.0%}: n={v['n']} 胜率{wr:.0f}% ${v['pnl']:+.2f}")

    msg = "\n".join(l for l in lines if l is not None)
    print(msg)
    if a.json_out:
        json.dump({**d, "generated": datetime.now().isoformat()},
                  open(a.json_out, "w"), ensure_ascii=False, default=str)
        print(f"\n→ {a.json_out}")


if __name__ == "__main__":
    main()
