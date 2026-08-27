#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BE 家族 §55 判据达标检查器 (2026-08-25, 只读零交易路径)。

背景 (DEB-20260825 第8轮 / 用户批"采集与交易分离"):
  - 每笔实盘(模拟口径)期望净 −$3.60 → 样本攒满后继续交易是纯失血;
  - 但 8/25 熔断定标回放证明"人为降速"会误伤盈利的亏损后再入场 → 不提前降速,
    攒满 §55 预注册 n≥30 后**精确停新开仓**, 引擎转纯采集模式 (资金流/盘前K线等
    攒样器均不需要成交, 停交易不影响十月管线进度)。
  - BE 家族定义 (方案A 落地起): exit_reason='SL_BREAKEVEN' AND substr(exit_time,1,10)>='2026-08-20'。

行为: 只读计数 + 写状态文件。达到 n≥30 时置 flag 并打印停机建议 —
  实际停开仓开关由人工/晨会确认后执行 (本脚本不直接改任何引擎配置)。
输出: stdout + data/be_family_gate_status.json (+ data/be_family_gate.flag 达标时)。
"""

import json
import sqlite3
from pathlib import Path

BASE = Path("/opt/ftseek/FTSeek-US")
DB = BASE / "data" / "ftseek_us.db"
STATUS = BASE / "data" / "be_family_gate_status.json"
FLAG = BASE / "data" / "be_family_gate.flag"

N_TARGET = 30  # §55 预注册判据


def main():
    conn = sqlite3.connect(DB)
    # 2026-08-28 口径统一: substr 而非 date() (后者把 -04:00 归一到 UTC,
    # 08-20 落地首日晚出场会被挪日) — 全仓"当日"=ET 墙钟日前 10 位。
    row = conn.execute(
        """SELECT COUNT(*), COALESCE(SUM(pnl_net),0),
                  SUM(CASE WHEN pnl_net>0 THEN 1 ELSE 0 END)
           FROM trade_records
           WHERE exit_reason='SL_BREAKEVEN'
             AND substr(exit_time,1,10)>='2026-08-20'"""
    ).fetchone()
    conn.close()
    n, net, wins = int(row[0]), round(float(row[1]), 2), int(row[2] or 0)
    remaining = max(0, N_TARGET - n)
    reached = remaining == 0

    status = {
        "checked": "be_family_sl_breakeven_since_2026-08-20",
        "n": n, "target": N_TARGET, "remaining": remaining,
        "net": net, "wins": wins,
        "gate_reached": reached,
        "action_if_reached": (
            "n>=30 已达标: 按 DEB-20260825 第8轮决议, 下个交易日 DA 计划不再产生新开仓指令 "
            "(引擎转纯采集模式); 本脚本只报状态, 开关须人工/晨会确认后执行"
        ),
    }
    STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=1))
    if reached:
        FLAG.write_text(json.dumps(status, ensure_ascii=False))

    print(f"[be-gate] n={n}/{N_TARGET} (还差 {remaining}) 净 {net} 胜 {wins}")
    if reached:
        print("[be-gate] 🛑 达标 → 建议停新开仓 (转纯采集), 待人工确认")
    else:
        print(f"[be-gate] 继续正常节奏攒样 (~{remaining} 笔)")


if __name__ == "__main__":
    main()
