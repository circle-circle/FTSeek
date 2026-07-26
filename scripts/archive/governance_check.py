#!/usr/bin/env python3
"""
governance_check.py — FTSeek-US 治理合规自动检查

用法:
    python scripts/governance_check.py

输出:
    pass/fail 列表 + 违规明细
    用于周复盘或 Agent 自检

来源: SYSTEM_OBJECTIVE.md §5-§7
"""

import sqlite3
import subprocess
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "ftseek_us.db"
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
    print(f"=== FTSeek-US 治理合规检查 ===")
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

    # 胜率
    cur.execute("""
        SELECT 
          ROUND(100.0*SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END)/COUNT(*),1) as wr,
          COUNT(*) as n
        FROM trade_records 
        WHERE date(entry_time) >= date('now','-30 days') AND realized_pnl IS NOT NULL AND realized_pnl != 0
    """)
    wr, n = cur.fetchone()
    check(f"胜率 (30天): {wr}% (目标≥50%)", wr is not None and wr >= 50,
          f"当前 {wr}%，样本 {n} 笔")

    # 日均收益
    cur.execute("""
        SELECT 
          ROUND(SUM(realized_pnl) / COUNT(DISTINCT date(entry_time)), 2) as avg_daily,
          COUNT(DISTINCT date(entry_time)) as days
        FROM trade_records 
        WHERE date(entry_time) >= date('now','-30 days') AND realized_pnl IS NOT NULL AND realized_pnl != 0
    """)
    avg_daily, days = cur.fetchone()
    check(f"日均收益 (30天): ${avg_daily} (目标≥$500)", avg_daily is not None and avg_daily >= 500,
          f"当前 ${avg_daily}/天，{days} 个交易日")

    # ─── 2. 反指标检查 (§7) ─────────────────────────────────────
    print("\n[2] 反指标")

    # 单笔最大亏损
    cur.execute("""
        SELECT MIN(realized_pnl), symbol FROM trade_records 
        WHERE date(entry_time) >= date('now','-7 days') AND realized_pnl IS NOT NULL
    """)
    worst, worst_sym = cur.fetchone()
    check(f"单笔最大亏损 (7天): ${worst} (阈值≤$500)",
          worst is None or worst >= -500,
          f"最差: {worst_sym} ${worst}")

    # 连续亏损天数
    cur.execute("""
        SELECT date(entry_time) as day, ROUND(SUM(realized_pnl),2) as pnl
        FROM trade_records 
        WHERE date(entry_time) >= date('now','-7 days') AND realized_pnl IS NOT NULL AND realized_pnl != 0
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

    # ─── 3. 系统稳定性检查 (§F2) ──────────────────────────────────
    print("\n[3] 系统稳定性")

    # 幽灵仓
    cur.execute("""
        SELECT COUNT(*) FROM open_positions 
        WHERE sync_status NOT IN ('activated', 'synced', 'filled')
    """)
    ghost_count = cur.fetchone()[0]
    check(f"异常仓位: {ghost_count} (阈值=0)", ghost_count == 0,
          f"{ghost_count} 个异常仓位")

    # Shadow 未关闭
    cur.execute("SELECT COUNT(*) FROM shadow_trades WHERE closed=0")
    shadow_open = cur.fetchone()[0]
    check(f"Shadow 未关闭: {shadow_open} (关注)", shadow_open < 50,
          f"{shadow_open} 笔 shadow 未关闭，可能需要运行 shadow_eod_replay")

    # ─── 4. 配置-代码一致性 (§F3) ─────────────────────────────────
    print("\n[4] 配置一致性")

    # 检查 YAML 中 options.enabled
    if YAML_PATH.exists():
        yaml_content = YAML_PATH.read_text()
        options_enabled = "enabled: false" in yaml_content and "options:" in yaml_content
        # 简单检查 options section 附近是否有 enabled: false
        import re
        match = re.search(r'options:\s*\n\s*enabled:\s*(true|false)', yaml_content)
        if match:
            opt_val = match.group(1)
            check(f"期权策略: options.enabled={opt_val}", opt_val == "false",
                  "期权已下线 (ADR-001)，如需启用需 RFC")
        else:
            print("  ⚠️  无法解析 options.enabled")
    else:
        print(f"  ⚠️  YAML 不存在: {YAML_PATH}")

    # ─── 5. 最近 commits 合规性 (§8) ──────────────────────────────
    print("\n[5] Git Commit 合规")

    try:
        result = subprocess.run(
            ["git", "log", "--oneline", "-10", "--since=7 days ago"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=10
        )
        commits = result.stdout.strip().split("\n") if result.stdout.strip() else []

        bad_commits = []
        high_risk_keywords = ["_execute_long", "_execute_short", "_close_position",
                              "risk/engine", "exit_policy", "enabled.*true"]
        required_tags = ["ADR-", "RFC-", "hotfix", "chore", "doc", "test", "fix"]

        for c in commits:
            # 简化检查：高风险关键词 + 无标签
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

    # ─── 6. SYSTEM_OBJECTIVE.md 存在性 ─────────────────────────────
    print("\n[6] 治理文件")

    obj_path = ROOT / "SYSTEM_OBJECTIVE.md"
    check("SYSTEM_OBJECTIVE.md 存在", obj_path.exists())

    agents_path = ROOT / "AGENTS.md"
    check("AGENTS.md 存在", agents_path.exists())

    claude_path = ROOT / "CLAUDE.md"
    if claude_path.exists():
        claude_content = claude_path.read_text()
        has_gov = "SYSTEM_OBJECTIVE" in claude_content
        check("CLAUDE.md 引用 SYSTEM_OBJECTIVE", has_gov)

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
