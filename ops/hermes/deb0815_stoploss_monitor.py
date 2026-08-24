#!/usr/bin/env python3
"""DEB-20260815 §11.4 硬止损监控 — no_agent cron 脚本.

基线: playbook_sim 全史累计净 -$3,340.06 (n=269, 截至 2026-08-14 收盘; §13.1 复算确认)。
止损线: -$4,840.06 (基线 -$1,500)。
触线动作: unified_config.yaml playbook.auto_execute → false (HotReloader 60s 内生效, 免重启),
  输出告警文本 (cron 推送)。状态文件防重复触发。
语义: 正常输出空 (静默); 仅触线/异常输出。
"""
import sqlite3
import sys
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
DB = REPO / "data" / "ftseek_us.db"
CFG = REPO / "config" / "unified_config.yaml"
STATE = REPO / "data" / "deb0815_stoploss_state.json"
STOP_LINE = -4840.06
BASELINE = -3340.06


def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    row = con.execute(
        "SELECT COUNT(*) n, SUM(pnl_net) net FROM trade_records "
        "WHERE strategy_id='playbook_sim' AND pnl_net IS NOT NULL"
    ).fetchone()
    con.close()
    n, net = row[0], float(row[1] or 0.0)

    import json
    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text())
        except Exception:
            state = {}
    if state.get("tripped"):
        # 已触发过: 保持静默 (配置已关, 人工介入后才恢复)
        return

    if net <= STOP_LINE:
        # 触线: 关闭 auto_execute (精确文本替换, 该键全文件唯一)
        text = CFG.read_text()
        new_text = text.replace("  auto_execute: true", "  auto_execute: false", 1)
        if new_text == text:
            # 已是 false 或格式异常 → 告警人工检查, 不猜
            print(f"🛑 DEB-20260815 硬止损触线! playbook_sim 累计净 {net:+.2f} ≤ {STOP_LINE} "
                  f"(基线 {BASELINE:+.2f}, 亏 {net - BASELINE:+.2f}), "
                  f"但 auto_execute 配置未按预期匹配, 请人工核查并关停自动执行!")
        else:
            CFG.write_text(new_text)
            print(f"🛑 DEB-20260815 硬止损触线, auto_execute 已关闭!\n"
                  f"playbook_sim 累计净 {net:+.2f} ≤ 止损线 {STOP_LINE} "
                  f"(基线 {BASELINE:+.2f}, 委托期亏 {net - BASELINE:+.2f})\n"
                  f"按 §11.4: 不留'再观察一天', 不等任何闸门。"
                  f"配置 playbook.auto_execute=false 已写入 (HotReloader ~60s 生效)。\n"
                  f"恢复需用户明确指令 + DEB 追加裁决。")
        STATE.write_text(json.dumps({
            "tripped": True, "net": round(net, 2), "n": n,
            "stop_line": STOP_LINE, "baseline": BASELINE,
        }, ensure_ascii=False, indent=1))
        return

    # 未触线: 静默
    return


if __name__ == "__main__":
    main()
