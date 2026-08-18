#!/usr/bin/env python3
"""DA 供给健康监控 — bridge 产出数 vs 落库数对账 (DEB-20260818 P1)。

背景 (8/17 事故): DA 07:17 ET 写入 17 条 gate-PASS 指令, 09:00 generate
UPSERT 覆盖导致落库归零, 1.6h 盲窗靠人工察觉, 10:37 重注入才恢复
(当日 4 赢家 +$284.69 全部来自重注入批次)。根因已修 (1f227f98 SQL 仲裁),
本监控是"发现机制"——再遇同类事故, 系统自己喊。

对账逻辑 (09:10 ET, 事故签名显形后/开盘前):
  上游A: bridge 文件 data/opportunity_scan_{ET今日}.json 候选数 (DA 产出权威)
  上游B: signal_snapshots.db DA 决策数 (独立源, 区分"gate全拒"vs"覆盖")
  下游:  playbook_instructions 当日 DA 行数 (date 列)

判定:
  🔴 RED  落库=0 且 bridge 候选>0          → 覆盖/断链事故签名, 立即钉钉告警
  🔴 RED  落库 < bridge×0.5 且 bridge≥4     → 大规模丢失, 钉钉告警
  ⚠️ YELLOW 落库 < bridge (差额>3)          → 超自然损耗, 仅日志不推送
  ✅ OK   其余 (gate/dedup 自然损耗 0-3 条为正常, 见历史基准)

纯只读 + 告警推送, 零执行路径改动。回滚 = 删本文件 + 删 crontab 行。

运行: PYTHONPATH=. venv/bin/python scripts/da_supply_reconcile_check.py [--dry]
退出码: 1=RED (cron-catchable), 0=其他。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
os.chdir(str(PROJECT_DIR))
sys.path.insert(0, str(PROJECT_DIR))

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_DIR / ".env")
    load_dotenv(PROJECT_DIR / ".env.us")
except Exception:
    pass

from app.utils.timezone import ET  # noqa: E402

DB_MAIN = PROJECT_DIR / "data" / "ftseek_us.db"
DB_SNAP = PROJECT_DIR / "data" / "signal_snapshots.db"
YELLOW_GAP = 3      # 落库 vs bridge 差额 ≤3 = gate/dedup 自然损耗 (历史基准 0-3)
RED_RATIO = 0.5     # 落库 < bridge×0.5 且 bridge≥4 = 大规模丢失


def _today_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _is_trading_day() -> bool:
    try:
        r = subprocess.run(
            [sys.executable, str(PROJECT_DIR / "scripts" / "trading_day_check.py"), "--market", "US"],
            capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return True  # gate 自身故障时不误拦监控


def _bridge_candidates(date: str):
    """返回 (候选数, 详情) 或 (None, 原因)。只认 trade_date 匹配的文件。"""
    path = PROJECT_DIR / "data" / f"opportunity_scan_{date}.json"
    if not path.exists():
        return None, f"bridge 文件不存在 ({path.name})"
    try:
        j = json.loads(path.read_text())
    except Exception as e:
        return None, f"bridge 文件解析失败: {e}"
    if j.get("trade_date") and j["trade_date"] != date:
        return None, f"bridge trade_date={j.get('trade_date')} ≠ {date} (陈旧)"
    return len(j.get("candidates", []) or []), "ok"


def _db_da_rows(date: str) -> int:
    """playbook_instructions 当日 DA 落库行数 (date 列, ET 口径)。"""
    conn = sqlite3.connect(f"file:{DB_MAIN}?mode=ro", uri=True, timeout=30)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM playbook_instructions "
            "WHERE date = ? AND decision_id LIKE 'DA-%'", (date,)).fetchone()[0]
    finally:
        conn.close()


def _snap_da_decisions(date: str) -> int:
    """signal_snapshots.db 当日 DA 决策数 (独立上游源)。"""
    if not DB_SNAP.exists():
        return -1
    conn = sqlite3.connect(f"file:{DB_SNAP}?mode=ro", uri=True, timeout=30)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM signal_snapshots "
            "WHERE trade_date = ? AND decision_id LIKE 'DA-%'", (date,)).fetchone()[0]
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    ap.add_argument("--date", help="覆盖 ET 日期 (回测用)")
    args = ap.parse_args()

    date = args.date or _today_et()
    if not args.date and not _is_trading_day():
        print(f"[skip] {date} 非交易日")
        return

    bridge_n, bridge_detail = _bridge_candidates(date)
    db_n = _db_da_rows(date)
    snap_n = _snap_da_decisions(date)

    print(f"[DA供给对账 {date}] bridge候选={bridge_n}({bridge_detail}) "
          f"| DB落库={db_n} | signal_snapshots决策={snap_n}")

    if bridge_n is None:
        # bridge 缺失是另一类故障 (DA 未跑), pre_market_check 5b 已覆盖, 此处不重复告警
        print("[skip] bridge 缺失, 交由 pre_market_check 5b 处置")
        return

    status, msg = "OK", ""
    gap = bridge_n - db_n
    if db_n == 0 and bridge_n > 0:
        status = "RED"
        msg = (f"🔴 DA供给断链/覆盖事故签名 ({date} ET)\n"
               f"bridge 产出 {bridge_n} 条候选, 但 playbook_instructions 落库 DA 行 = 0\n"
               f"signal_snapshots 决策数 = {snap_n}\n"
               f"8/17 同款事故: generate UPSERT 覆盖致 DA 供给归零。\n"
               f"立即检查: ①日志 grep UPSERT/覆盖 ②必要时人工重注入 bridge。")
    elif bridge_n >= 4 and db_n < bridge_n * RED_RATIO:
        status = "RED"
        msg = (f"🔴 DA供给大规模丢失 ({date} ET)\n"
               f"bridge 产出 {bridge_n} 条, 落库仅 {db_n} 条 (差额 {gap}, "
               f"超出自然损耗阈值 {YELLOW_GAP})\n"
               f"signal_snapshots 决策数 = {snap_n}")
    elif gap > YELLOW_GAP:
        status = "YELLOW"
        msg = f"⚠️ DA 落库 {db_n} < bridge {bridge_n} (差 {gap} > {YELLOW_GAP}), 疑似部分丢失"

    print(f"[判定] {status} {msg}".rstrip())

    if status == "RED":
        if args.dry:
            print("[dry-run] 不推送")
        else:
            try:
                from app.notifier.dingtalk import DingTalkNotifier
                DingTalkNotifier().send_text(f"[FTSeek-US告警] {msg}")
                print("[ok] 钉钉已推送")
            except Exception as e:
                print(f"[warn] 钉钉推送失败: {e}")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
