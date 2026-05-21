#!/usr/bin/env python3
"""
governance_check.py — FTSeek 港股治理合规自动检查

用法:
    python3.8 scripts/governance_check.py

输出:
    pass/fail 列表 + 违规明细
    用于周复盘或 Agent 自检

来源: SYSTEM_OBJECTIVE.md §5-§7
"""

import sqlite3
import subprocess
import sys
import os
import re
from pathlib import Path
from datetime import datetime, timedelta

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "ftseek.db"
SIM_DB_PATH = ROOT / "data" / "simulation.db"
YAML_PATH = ROOT / "config" / "unified_config.yaml"

results = []

def check(name: str, passed: bool, detail: str = ""):
    """记录一项检查结果"""
    status = "✅ PASS" if passed else "❌ FAIL"
    results.append((name, passed, detail))
    print(f"  {status}: {name}")
    if detail and not passed:
        print(f"         {detail}")


def main():
    print(f"=== FTSeek 港股治理合规检查 ===")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"DB: {DB_PATH}")
    print()

    # ─── 1. 北极星指标检查 (§2) ─────────────────────────────────
    print("[1] 北极星指标")

    if not DB_PATH.exists():
        print(f"  ⚠️  DB 不存在: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # 使用 positions 表 (trade_records 可能为空)
    # 胜率: 已平仓 (status='CLOSED') 且 realized_pnl > 0
    cur.execute("""
        SELECT 
          ROUND(100.0*SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr,
          COUNT(*) as n
        FROM positions 
        WHERE status='CLOSED' AND realized_pnl IS NOT NULL AND realized_pnl != 0
    """)
    wr, n = cur.fetchone()
    if n and n > 0:
        check(f"胜率 (全量): {wr}% (目标≥50%)", wr is not None and wr >= 50,
              f"当前 {wr}%，样本 {n} 笔")
    else:
        check("胜率 (全量): 无数据", False, "positions 表无已平仓记录，样本不足")

    # 日均收益
    cur.execute("""
        SELECT 
          ROUND(SUM(realized_pnl) / COUNT(DISTINCT date(exit_time)), 2) as avg_daily,
          COUNT(DISTINCT date(exit_time)) as days
        FROM positions 
        WHERE status='CLOSED' AND realized_pnl IS NOT NULL AND realized_pnl != 0
    """)
    avg_daily, days = cur.fetchone()
    if days and days > 0:
        check(f"日均收益: HK${avg_daily} (目标≥HK$500)", avg_daily is not None and avg_daily >= 500,
              f"当前 HK${avg_daily}/天，{days} 个交易日")
    else:
        check("日均收益: 无数据", False, "positions 表无已平仓记录")

    # ─── 2. 反指标检查 (§7) ─────────────────────────────────────
    print("\n[2] 反指标")

    # 单笔最大亏损
    cur.execute("""
        SELECT MIN(realized_pnl), code FROM positions 
        WHERE status='CLOSED' AND realized_pnl IS NOT NULL
    """)
    worst, worst_sym = cur.fetchone()
    if worst is not None:
        check(f"单笔最大亏损: HK${worst} (阈值≤HK$500)",
              worst >= -500,
              f"最差: {worst_sym} HK${worst}")
    else:
        check("单笔最大亏损: 无数据", True, "无已平仓记录")

    # 连续亏损天数
    cur.execute("""
        SELECT date(exit_time) as day, ROUND(SUM(realized_pnl),2) as pnl
        FROM positions 
        WHERE status='CLOSED' AND realized_pnl IS NOT NULL AND realized_pnl != 0
        GROUP BY day ORDER BY day DESC
    """)
    days_data = cur.fetchall()
    consecutive_loss = 0
    for day, pnl in days_data:
        if pnl < 0:
            consecutive_loss += 1
        else:
            break
    check(f"连续亏损天数: {consecutive_loss} (阈值≤3)", consecutive_loss <= 3,
          f"连续 {consecutive_loss} 天亏损")

    # ─── 3. 系统稳定性检查 ────────────────────────────────────────
    print("\n[3] 系统稳定性")

    # 幽灵仓: status 不在正常范围
    cur.execute("""
        SELECT COUNT(*) FROM positions 
        WHERE status NOT IN ('OPEN', 'CLOSED')
    """)
    ghost_count = cur.fetchone()[0]
    check(f"异常仓位: {ghost_count} (阈值=0)", ghost_count == 0,
          f"{ghost_count} 个异常仓位")

    # Shadow 未关闭 (simulation.db)
    if SIM_DB_PATH.exists():
        sim_conn = sqlite3.connect(str(SIM_DB_PATH))
        sim_cur = sim_conn.cursor()
        # simulation_positions 没有 status 列，所有记录视为 open
        sim_cur.execute("SELECT COUNT(*) FROM simulation_positions")
        shadow_open = sim_cur.fetchone()[0]
        check(f"Shadow 未关闭: {shadow_open} (关注)", shadow_open < 50,
              f"{shadow_open} 笔 shadow 记录")
        sim_conn.close()
    else:
        check("Shadow 未关闭: 无 simulation.db", True, "")

    # ─── 4. 配置-代码一致性 ─────────────────────────────────────
    print("\n[4] 配置一致性")

    if YAML_PATH.exists():
        yaml_content = YAML_PATH.read_text()

        # 检查 SSOT 审计脚本
        audit_script = ROOT / "scripts" / "config_audit.py"
        if audit_script.exists():
            try:
                result = subprocess.run(
                    ["python3.8", str(audit_script), "FTSeek"],
                    capture_output=True, text=True, cwd=str(ROOT), timeout=30
                )
                check("SSOT 配置审计", result.returncode == 0,
                      result.stdout.strip()[:200] if result.returncode != 0 else "")
            except Exception as e:
                print(f"  ⚠️  配置审计执行失败: {e}")
        else:
            print("  ⚠️  scripts/config_audit.py 不存在，跳过")
    else:
        print(f"  ⚠️  YAML 不存在: {YAML_PATH}")

    # ─── 5. 最近 commits 合规性 ──────────────────────────────────
    print("\n[5] Git Commit 合规")

    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-10", "--since=7 days ago"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=10
        )
        commits = result.stdout.strip().split("\n") if result.stdout.strip() else []

        bad_commits = []
        high_risk_keywords = ["dynamic_exit", "exit_executor", "data_subscriber",
                              "trailing_stop", "breakeven", "enabled.*true"]
        required_tags = ["ADR-", "RFC-", "hotfix", "chore", "doc", "test", "fix"]

        for c in commits:
            msg = c.lower()
            has_risk = any(kw.lower() in msg for kw in high_risk_keywords)
            has_tag = any(tag in c for tag in required_tags)
            if has_risk and not has_tag:
                bad_commits.append(c)

        check(f"近 7 天 commits: {len(commits)} 个，{len(bad_commits)} 个无标签",
              len(bad_commits) == 0,
              "\n".join(bad_commits) if bad_commits else "")
    except Exception as e:
        print(f"  ⚠️  Git 检查失败: {e}")

    # ─── 6. 治理文件检查 ─────────────────────────────────────────
    print("\n[6] 治理文件")

    for fname in ["SYSTEM_OBJECTIVE.md", "AGENT_INTERACTION.md", "AGENTS.md", "CLAUDE.md"]:
        fpath = ROOT / fname
        check(f"{fname} 存在", fpath.exists())

    # 检查 CLAUDE.md 是否引用 SYSTEM_OBJECTIVE
    claude_path = ROOT / "CLAUDE.md"
    if claude_path.exists():
        claude_content = claude_path.read_text()
        has_gov = "SYSTEM_OBJECTIVE" in claude_content
        check("CLAUDE.md 引用 SYSTEM_OBJECTIVE", has_gov)

    # ─── 7. Git Hooks 检查 ───────────────────────────────────────
    print("\n[7] Git Hooks")

    hooks_dir = ROOT / ".git" / "hooks"
    for hook_name in ["pre-commit", "commit-msg"]:
        hook_path = hooks_dir / hook_name
        exists = hook_path.exists()
        executable = os.access(str(hook_path), os.X_OK) if exists else False
        check(f"{hook_name} hook {'存在且可执行' if executable else '缺失或不可执行'}",
              exists and executable)

    # ─── 汇总 ────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    passed = sum(1 for _, p, _ in results if p)
    failed = sum(1 for _, p, _ in results if not p)
    print(f"总计: {passed} 通过 / {failed} 失败")

    if failed > 0:
        print("\n失败项:")
        for name, p, detail in results:
            if not p:
                print(f"  ❌ {name}")
                if detail:
                    print(f"     {detail}")

    conn.close()
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
