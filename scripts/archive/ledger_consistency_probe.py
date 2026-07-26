#!/usr/bin/env python3
"""账本一致性只读巡检 (2026-07-16, 方式1 被动观察)

用途: 在真实/模拟成交发生后, 核对三方账本是否一致, 不一致则告警。
  三方: (a) broker 持仓  — 从服务对账日志解析最近一次计数 (不直连 OpenD, 避免抢连接)
        (b) open_positions — DB 权威持仓表 (仅活跃行)
        (c) trade_records  — DB 流水表 (exit_time IS NULL = 未平)

安全: 纯只读。不连 broker, 不下单, 不改 DB。仅读日志 + DB, 输出比对结果。
      发现不一致 → 退出码 1 (供 cron/error_alert 捕获); 一致 → 退出码 0。

由 cron 盘中定时执行, 或手动跑。
"""
import os, re, sys, sqlite3
from datetime import datetime

os.chdir('/opt/ftseek/FTSeek-US')

DB_MAIN = os.getenv('PROBE_DB_MAIN', 'data/ftseek_us.db')
DB_LIVE = os.getenv('PROBE_DB_LIVE', 'data/ftseek_us_live.db')
LOG = os.getenv('PROBE_LOG', 'logs/ftseek_us.log')

# playbook 仓由 exit monitor 独占管理, 不纳入 broker 对账不一致判定
PLAYBOOK_SOURCES = ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')


def _tr_open_positions(db):
    """trade_records 中未平仓记录 (exit_time IS NULL)。"""
    out = []
    try:
        c = sqlite3.connect(db); c.row_factory = sqlite3.Row
        cur = c.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='trade_records'")
        if cur.fetchone():
            cur.execute(
                "SELECT id, symbol, direction, quantity, entry_time, source_type "
                "FROM trade_records WHERE exit_time IS NULL OR exit_time=''"
            )
            out = [dict(r) for r in cur.fetchall()]
        c.close()
    except Exception as e:
        print(f"[probe] 读 {db} trade_records 异常: {e}")
    return out


def _open_positions_active(db):
    """open_positions 中活跃行 (size!=0 且未被 sync 标记关闭)。"""
    out = []
    try:
        c = sqlite3.connect(db); c.row_factory = sqlite3.Row
        cur = c.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='open_positions'")
        if cur.fetchone():
            cur.execute(
                "SELECT symbol, direction, size, sync_status FROM open_positions "
                "WHERE ABS(COALESCE(size,0)) > 0.01 AND ("
                "sync_status IS NULL OR sync_status NOT IN ("
                "'closed_by_daemon','closed_by_scheduler','closed_by_sync',"
                "'closed_emergency','stale_not_in_broker'))"
            )
            out = [dict(r) for r in cur.fetchall()]
        c.close()
    except Exception as e:
        print(f"[probe] 读 {db} open_positions 异常: {e}")
    return out


def _last_broker_count():
    """从服务对账日志解析最近一次 broker 持仓计数 + 时间戳。"""
    pat = re.compile(r'^([\d\-]+\s[\d:,]+).*Broker 持仓:\s*(\d+)\s*\|\s*本地内存:\s*(\d+)')
    last = None
    try:
        with open(LOG, 'r', errors='ignore') as f:
            for line in f:
                m = pat.search(line)
                if m:
                    last = (m.group(1), int(m.group(2)), int(m.group(3)))
    except Exception as e:
        print(f"[probe] 读日志异常: {e}")
    return last


def main():
    print(f"=== 账本一致性巡检 @ {datetime.now().isoformat()} ===")
    issues = []

    # (c) trade_records 未平 (排除 playbook, 由 exit monitor 管)
    tr_all = _tr_open_positions(DB_MAIN) + _tr_open_positions(DB_LIVE)
    tr_non_pb = [r for r in tr_all if (r.get('source_type') or '').upper() not in PLAYBOOK_SOURCES]
    tr_pb = [r for r in tr_all if (r.get('source_type') or '').upper() in PLAYBOOK_SOURCES]
    print(f"[c] trade_records 未平: 共 {len(tr_all)} (非playbook {len(tr_non_pb)}, playbook {len(tr_pb)})")
    for r in tr_non_pb:
        print(f"    ⚠ 非playbook未平: id={r['id']} {r['symbol']} {r['direction']} "
              f"qty={r['quantity']} src={r['source_type']} entry={r['entry_time']}")

    # (b) open_positions 活跃
    op = _open_positions_active(DB_MAIN) + _open_positions_active(DB_LIVE)
    print(f"[b] open_positions 活跃: {len(op)}")
    for r in op:
        print(f"    {r['symbol']} {r['direction']} size={r['size']} sync={r['sync_status']}")

    # (a) broker (来自服务日志)
    bc = _last_broker_count()
    if bc:
        print(f"[a] broker 持仓 (服务对账 @ {bc[0]}): broker={bc[1]} 本地内存={bc[2]}")
    else:
        print("[a] broker 持仓: 未在日志中找到对账计数 (服务可能未跑对账)")

    # ── 一致性判定 ──
    # 规则1: 非playbook 的 trade_records 未平, 但 broker/open_positions 都没有 → 孤儿幻影
    op_syms = {r['symbol'] for r in op}
    broker_n = bc[1] if bc else None
    for r in tr_non_pb:
        if r['symbol'] not in op_syms and broker_n == 0:
            issues.append(
                f"孤儿幻影: trade_records id={r['id']} {r['symbol']} 未平, "
                f"但 open_positions 无此仓 且 broker=0"
            )

    # 规则2: open_positions 活跃数 > broker 数 (日志) → open_positions 残留
    if broker_n is not None and len(op) > broker_n:
        issues.append(f"open_positions 残留: 活跃 {len(op)} > broker {broker_n}")

    print("─" * 50)
    if issues:
        print(f"❌ 发现 {len(issues)} 处账本不一致:")
        for i in issues:
            print(f"   - {i}")
        return 1
    print("✅ 三方账本一致 (或无持仓)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
