#!/usr/bin/env python3
"""
Phase B 启动条件验证 CLI (2026-05-15)

检查 Phase B 各组件触发条件 + 当前状态:
- B.1 ExitDispatcher 优先调用情况
- B.2 ExitPolicy thesis-aware (PB-3)
- B.3 SignalArbitrator enforce 条件 (n_candidates>1 数据)
- B.4 SymbolLock enforce 条件 (OBSERVE_CONFLICT 次数)
- B.5 CapitalAllocator enforce 条件 (BLOCKED_*_CAP 次数)
- B.6 thesis-driven exit (entry_thesis 落库率)

用法:
  python scripts/phase_b_status.py            # 当前状态
  python scripts/phase_b_status.py --days 7   # 7 天滚动数据
"""
from __future__ import annotations
import argparse
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _check_dispatcher():
    """B.1 ExitDispatcher 状态"""
    print("\n=== B.1 ExitDispatcher ===")
    try:
        from app.coordinator.exit_dispatcher import get_exit_dispatcher
        dp = get_exit_dispatcher()
        registered = dp.all_strategies()
        print(f"  注册的 ExitPolicy: {registered}")
        print(f"  状态: {'✅ 已激活' if registered else '⚠ 未注册'}")
    except Exception as e:
        print(f"  ❌ 异常: {e}")


def _check_thesis_inferer():
    """B.6 thesis_inferer 落库率"""
    print("\n=== B.6 entry_thesis 落库率 ===")
    try:
        from app.core.paths import get_trade_records_db
        conn = sqlite3.connect(str(get_trade_records_db()))
        rows = conn.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN entry_thesis IS NOT NULL AND entry_thesis != '' THEN 1 ELSE 0 END) as with_thesis,
                COUNT(DISTINCT entry_thesis) as distinct_theses
            FROM trade_attribution
            WHERE date(created_at) = date('now')
        """).fetchone()
        total, with_thesis, distinct = rows
        rate = (with_thesis / total * 100) if total else 0
        print(f"  今日入场: {total} 笔")
        print(f"  含 entry_thesis: {with_thesis} ({rate:.0f}%)")
        print(f"  thesis 类型: {distinct}")
        if total > 0:
            theses = conn.execute("""
                SELECT entry_thesis, COUNT(*) FROM trade_attribution
                WHERE date(created_at) = date('now') AND entry_thesis IS NOT NULL
                GROUP BY 1
            """).fetchall()
            for t, n in theses:
                print(f"    - {t}: {n}")
        conn.close()

        from app.llm.features.feature_flags import is_enabled
        en = is_enabled("thesis_inferer")
        print(f"  L3 yaml 启用: {'✅' if en else '❌'}")
    except Exception as e:
        print(f"  ❌ 异常: {e}")


def _check_arbitrator(days: int):
    """B.3 SignalArbitrator 数据"""
    print(f"\n=== B.3 SignalArbitrator ({days}d) ===")
    try:
        from app.core.paths import get_decisions_db
        conn = sqlite3.connect(str(get_decisions_db()))
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_arbitration'"
        ).fetchone()
        if not cur:
            print("  ⚠ signal_arbitration 表尚未创建 (Coordinator 未运行)")
            conn.close()
            return
        rows = conn.execute(f"""
            SELECT
                COUNT(*) as total,
                COALESCE(SUM(CASE WHEN n_candidates > 1 THEN 1 ELSE 0 END), 0) as multi,
                COUNT(DISTINCT winner_strategy) as winners
            FROM signal_arbitration
            WHERE timestamp > strftime('%s', 'now', ?)
        """, (f'-{days} days',)).fetchone()
        total, multi, winners = rows
        print(f"  总仲裁: {total}")
        print(f"  多候选 (n>1): {multi}")
        print(f"  winner 分布: {winners} 种 strategy")
        # 启动判定
        if multi >= 10:
            print(f"  ✅ 满足 enforce 切换条件 (multi >= 10)")
        else:
            print(f"  ⏸ 未达到 enforce 切换 (需 multi >= 10, 当前 {multi})")
        conn.close()
    except Exception as e:
        print(f"  ❌ 异常: {e}")


def _check_observe_events():
    """B.4/B.5 observe 事件 (从 journal log 读, 简化为 CLI 提示)"""
    print("\n=== B.4 / B.5 OBSERVE 事件 ===")
    print("  CapitalAllocator BLOCKED_* 次数: 需查 journal log")
    print("  SymbolLock OBSERVE_CONFLICT 次数: 需查 journal log")
    print("  命令: journalctl -u ftseek-us --since today | grep -cE 'BLOCKED_(STRATEGY|TOTAL)_CAP|OBSERVE_CONFLICT'")
    print("  enforce 启动门槛: BLOCKED > 3 OR CONFLICT > 5")


def _check_phase_b_readiness():
    """综合判定"""
    print("\n=== Phase B 启动综合判定 ===")
    try:
        from app.core.paths import get_decisions_db, get_trade_records_db
        conn = sqlite3.connect(str(get_decisions_db()))
        try:
            multi = conn.execute("""
                SELECT COUNT(*) FROM signal_arbitration
                WHERE n_candidates > 1
                  AND timestamp > strftime('%s', 'now', '-7 days')
            """).fetchone()[0]
        except Exception:
            multi = 0
        conn.close()

        conn = sqlite3.connect(str(get_trade_records_db()))
        thesis_rate = conn.execute("""
            SELECT
                CAST(SUM(CASE WHEN entry_thesis IS NOT NULL AND entry_thesis != '' THEN 1 ELSE 0 END) AS REAL)
                / NULLIF(COUNT(*), 0)
            FROM trade_attribution
            WHERE date(created_at) >= date('now', '-7 days')
        """).fetchone()[0] or 0
        conn.close()

        ready_b6 = thesis_rate >= 0.5
        ready_b3 = multi >= 10
        ready_b4_b5 = False  # 需手工查 journal

        print(f"  B.6 thesis-driven exit: {'✅' if ready_b6 else '⏸'} (thesis 落库率 {thesis_rate*100:.0f}%, 需 ≥50%)")
        print(f"  B.3 Arbitrator enforce: {'✅' if ready_b3 else '⏸'} (multi {multi}, 需 ≥10)")
        print(f"  B.4/B.5 Lock/Capital enforce: ⏸ (需手工查 journal)")
        print()
        print("  → 当前可启动:")
        if ready_b6: print("    ✅ B.6 thesis-driven exit (启用 PB-3 升级 ExitPolicy)")
        if ready_b3: print("    ✅ B.3 SignalArbitrator enforce (yaml 切 strict_mode)")
        print()
        print("  → Phase C 启动建议: 至少 7 天 baseline 数据 + Phase B.1-B.4 全部稳定")
    except Exception as e:
        print(f"  ❌ 异常: {e}")


def main():
    ap = argparse.ArgumentParser(description="Phase B 启动条件验证")
    ap.add_argument("--days", type=int, default=1)
    args = ap.parse_args()

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Phase B Status")
    print("=" * 70)
    _check_dispatcher()
    _check_thesis_inferer()
    _check_arbitrator(args.days)
    _check_observe_events()
    _check_phase_b_readiness()


if __name__ == "__main__":
    main()
