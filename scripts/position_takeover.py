#!/usr/bin/env python3
"""持仓接管/释放/冻结 CLI (M1, 2026-05-16)

用法:
    python scripts/position_takeover.py list                    # 列出所有 PROTECTED/FROZEN
    python scripts/position_takeover.py takeover NVDA "原因"     # PROTECTED → MANAGED
    python scripts/position_takeover.py release NVDA "原因"      # MANAGED → PROTECTED
    python scripts/position_takeover.py freeze NVDA "原因"       # → FROZEN (临时停操作)
    python scripts/position_takeover.py unfreeze NVDA            # FROZEN → PROTECTED
    python scripts/position_takeover.py snapshot                # 全量风险敞口 (含全 source)

铁律:
- 不直接修改 broker 持仓
- 仅修改 PositionStore 元数据 (策略读此元数据决定接管行为)
- 所有变更落 decisions.db.position_protection_log 留痕
"""
import os
import sys
import argparse

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_DIR)


def _cmd_list(args):
    """列出受保护仓位"""
    # 注意: CLI 独立进程, 无法读运行中服务的 PositionStore.
    # 这里查 open_positions DB 表 (PositionStore 持久化层).
    import sqlite3
    db_path = os.path.join(PROJECT_DIR, "data", "ftseek_us.db")
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT symbol, direction, size, avg_entry_price, "
        "owner_strategy, trader_type, is_anomalous "
        "FROM open_positions WHERE is_filled=1 ORDER BY symbol"
    ).fetchall()
    conn.close()

    print(f"{'Symbol':<10} {'Dir':<6} {'Size':>8} {'Cost':>10} {'Owner':<12} {'Trader':<12}")
    print("-" * 70)
    for r in rows:
        print(f"{r[0]:<10} {r[1]:<6} {r[2]:>8.0f} {r[3]:>10.2f} "
              f"{(r[4] or '-'):<12} {(r[5] or '-'):<12}")
    print(f"\n总计 {len(rows)} 仓位.")
    print("注: protection 状态仅在运行中服务可见, 此 CLI 显示 broker 端字段.")


def _cmd_action(args):
    """takeover / release / freeze / unfreeze — 写 protection_log 留痕"""
    # 直接写 audit DB. 运行中服务下次 reload metadata 时生效.
    # (M1 限制: CLI 不能直通运行中的 PositionStore 单例 — 跨进程)
    import sqlite3
    from datetime import datetime

    audit_db = os.path.join(PROJECT_DIR, "data", "decisions.db")
    state_map = {
        "takeover": ("MANAGED", "用户接管"),
        "release":  ("PROTECTED", "用户释放"),
        "freeze":   ("FROZEN", "临时冻结"),
        "unfreeze": ("PROTECTED", "解冻"),
        "adopt":    ("MANAGED", "M1.1: 识别为策略孤儿, 接管监控"),
    }
    target, default_reason = state_map[args.action]

    conn = sqlite3.connect(audit_db)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS position_protection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT,
            reason TEXT,
            actor TEXT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "INSERT INTO position_protection_log "
        "(symbol, from_state, to_state, reason, actor) VALUES (?,?,?,?,?)",
        (args.symbol, "?", target, args.reason or default_reason, "cli")
    )
    conn.commit()
    conn.close()

    print(f"✅ {args.symbol} → {target} (reason={args.reason or default_reason})")
    print(f"   已写 decisions.db.position_protection_log")
    print()
    print("⚠️  生效说明:")
    print("   1. 运行中服务读 PositionStore 内存元数据 — CLI 无法跨进程修改")
    print("   2. 立即生效需重启服务: systemctl restart ftseek-us")
    print("   3. 或等下次 30s position_sync 重读 (尚未实现, 待 M2)")


def _cmd_snapshot(args):
    """全量风险敞口快照 — 跨 source"""
    # 这里直接读 open_positions DB 表算
    import sqlite3
    db_path = os.path.join(PROJECT_DIR, "data", "ftseek_us.db")
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT symbol, direction, size, avg_entry_price, "
        "owner_strategy, trader_type FROM open_positions WHERE is_filled=1"
    ).fetchall()
    conn.close()

    by_source = {}
    by_dir = {"LONG": 0.0, "SHORT": 0.0}
    total_mv = 0.0

    for r in rows:
        symbol, direction, size, price, owner, trader = r
        mv = abs(float(size or 0) * float(price or 0))
        # source 优先 owner, fallback trader
        src = (owner or trader or "unknown").lower()
        by_source[src] = by_source.get(src, 0.0) + mv
        if direction:
            by_dir[direction.upper()] = by_dir.get(direction.upper(), 0.0) + mv
        total_mv += mv

    print(f"\n{'='*70}")
    print(f"风险敞口快照 (跨 source)")
    print(f"{'='*70}")
    print(f"总名义价值: ${total_mv:,.2f}")
    print(f"持仓数:     {len(rows)}")
    print(f"\n按方向:")
    for d, v in by_dir.items():
        print(f"  {d:6}: ${v:>12,.2f}")
    print(f"\n按 source:")
    for s, v in sorted(by_source.items(), key=lambda x: -x[1]):
        print(f"  {s:20}: ${v:>12,.2f} ({100*v/total_mv:5.1f}%)")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="持仓保护状态 CLI (M1, 2026-05-16)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("list", help="列出全部持仓")
    sub.add_parser("snapshot", help="风险敞口快照")

    for action in ("takeover", "release", "freeze", "unfreeze", "adopt"):
        p = sub.add_parser(action, help=f"{action} 持仓")
        p.add_argument("symbol", help="标的代码 (e.g. US.NVDA)")
        p.add_argument("reason", nargs="?", default="", help="变更原因")

    args = parser.parse_args()

    if args.cmd == "list":
        _cmd_list(args)
    elif args.cmd == "snapshot":
        _cmd_snapshot(args)
    elif args.cmd in ("takeover", "release", "freeze", "unfreeze", "adopt"):
        args.action = args.cmd
        _cmd_action(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
