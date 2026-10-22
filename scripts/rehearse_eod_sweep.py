#!/usr/bin/env python3
"""EOD 兜底演练器 (只读, 零下单): 用生产库快照 + 假券商真跑 _force_close_orphaned_sim_positions。

为什么需要: EOD 兜底的行为只在 AFTER_HOURS 窗口可见, 改完代码到真窗口之间有十几个小时
空档 —— 期间"已修好"只能靠读码和单测。本器把真函数 + 真数据 + 可控时钟拼起来, 5 秒内
给出四个时点的实际交腿结果, 用于**先证伪再上线**。

刻意桩掉的三处 (否则演练会污染生产或碰钱链):
  · `get_dual_broker()` → 假券商 (记录 legs, 永不下单)
  · `emit_eod_depth_gate_shadow()` → 记录器 (不写 data/shadow/*.jsonl)
  · `now_et()` / `get_market_phase()` → 可控时钟/相位
数据面走 **sqlite3 backup API** 取一致性快照 (WAL 下 `cp .db` 会漏未 checkpoint 的事务;
实测漏掉 TSLA 行 ⇒ 基线假绿, 是本器最容易踩的坑)。

用法:
    PYTHONPATH=. venv/bin/python3.11 scripts/rehearse_eod_sweep.py            # 四个标准时点
    PYTHONPATH=. venv/bin/python3.11 scripts/rehearse_eod_sweep.py --json     # 机读
    PYTHONPATH=. venv/bin/python3.11 scripts/rehearse_eod_sweep.py --db <copy.db>

判读: S0=修前基线(应复现 P0 形态); S1=同刻修后(应 0 腿); S2=收盘窗内(应正常平非豁免仓);
S3=窗口外(应 0 腿, 只留豁免)。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from shutil import rmtree

ROOT = Path(__file__).resolve().parent.parent
os.chdir(str(ROOT))
sys.path.insert(0, str(ROOT))

logger = logging.getLogger("ftseek.scripts.rehearse_eod_sweep")

QUOTES = {"US.TSLA": 386.47, "US.ARM": 226.59, "US.BMY": 61.12,
          "US.RIVN": 12.46, "US.MSFT": 532.95}   # 演练基准价 (2026-10-22 快照)


class _FakeDual:
    def __init__(self):
        self.legs = []

    def close_position(self, symbol, qty, direction, **kw):
        self.legs.append({"symbol": symbol, "qty": qty, "direction": direction})
        return {"success": True, "exit_state": "BOOKED", "filled_price": QUOTES.get(symbol, 0)}


class _FakeRouter:
    @staticmethod
    def get_quote(symbol):
        return {"last": QUOTES.get(symbol, 0)}


class _FakeScheduler:
    data_router = _FakeRouter()


def _snapshot(src: Path) -> Path:
    """WAL 安全快照 (见模块 docstring 的坑)。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="eod_rehearsal_"))
    dst = tmpdir / "ftseek_us.db"
    s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    return dst


def _run(label, now_et, phase, db_path, *, baseline=False):
    import app.core.paths as paths
    import app.core.scheduler_triggers as st
    import app.execution.dual_broker as dbmod
    import app.playbook.overnight_decision as od
    import app.utils.timezone as tz
    from app.playbook.pipeline_parts import overnight_cleanup as oc

    hits, alerts, shadow = [], [], []
    fake = _FakeDual()
    # 逐项显式还原, 防演练变量泄漏到同进程后续调用
    saved = (tz.now_et, st.get_market_phase, dbmod.get_dual_broker,
             paths.get_trade_records_db, od.emit_eod_depth_gate_shadow, oc.eod_close_verdict)
    try:
        if baseline:   # 复现修前行为: 守卫恒 submit
            oc.eod_close_verdict = lambda *a, **k: ("submit", "BASELINE(修前)")
        tz.now_et = lambda: now_et
        st.get_market_phase = lambda: phase
        dbmod.get_dual_broker = lambda: fake
        paths.get_trade_records_db = lambda: str(db_path)
        od.emit_eod_depth_gate_shadow = lambda *a, **k: shadow.append(a[1] if len(a) > 1 else "?")
        closed = oc._force_close_orphaned_sim_positions(
            _FakeScheduler(), logger=logger,
            _alert_close_failure=lambda sym, env, reason, detail, **kw: alerts.append(
                {"symbol": sym, "reason": reason}),
            _book_playbook_exit_result=lambda *a, **k: 1,
            _cohort_b_max_hold_hit=lambda _t: False,
            _mark_overnight_arm=lambda *a, **k: None,
            _playbook_exit_decision=lambda reason, price, meta: {"reason": reason, "metadata": {}},
            _playbook_exit_owner=lambda db, r, meta: {"kind": "trade"},
            _release_live_broker_stop_for_rule_exit=lambda *a, **k: {"action": "proceed"},
            _resume_playbook_exits=lambda allow_submit: (0, {}, True),
            _today_et_str=lambda: now_et.strftime("%Y-%m-%d"),
        )
    finally:
        (tz.now_et, st.get_market_phase, dbmod.get_dual_broker,
         paths.get_trade_records_db, od.emit_eod_depth_gate_shadow,
         oc.eod_close_verdict) = saved
    return {"label": label, "now_et": now_et.isoformat(timespec="minutes"),
            "phase": phase.value, "closed": closed, "legs": fake.legs,
            "alerts": alerts, "exempt_or_shadow": shadow}


def main(argv=None):
    ap = argparse.ArgumentParser(description="EOD 兜底只读演练")
    ap.add_argument("--db", default="", help="演练库快照 (缺省=从 data/ftseek_us.db 现取)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    from app.core.scheduler_triggers import MarketPhase
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")

    own = not args.db
    db_path = _snapshot(ROOT / "data" / "ftseek_us.db") if own else Path(args.db)
    try:
        open_rows = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True).execute(
            "SELECT symbol,entry_time FROM trade_records WHERE source_type='PLAYBOOK_SIM' "
            "AND (exit_price=0 OR exit_price IS NULL OR exit_time='' OR exit_time IS NULL)"
        ).fetchall()
        scenarios = [
            ("S0 基线·修前 (历史盘后建仓时刻)", datetime(2026, 10, 21, 16, 45, 53, tzinfo=ET),
             MarketPhase.AFTER_HOURS, True),
            ("S1 修后·同刻重放", datetime(2026, 10, 21, 16, 45, 53, tzinfo=ET),
             MarketPhase.AFTER_HOURS, False),
            ("S2 修后·收盘窗内 16:05", datetime(2026, 10, 22, 16, 5, tzinfo=ET),
             MarketPhase.AFTER_HOURS, False),
            ("S3 修后·窗口外 16:45", datetime(2026, 10, 22, 16, 45, tzinfo=ET),
             MarketPhase.AFTER_HOURS, False),
        ]
        runs = [_run(lbl, when, phase, db_path, baseline=base)
                for lbl, when, phase, base in scenarios]
    finally:
        if own:
            rmtree(db_path.parent, ignore_errors=True)

    if args.json:
        print(json.dumps({"open_rows": open_rows, "runs": runs}, ensure_ascii=False, indent=2))
        return 0

    print("演练库未平 PLAYBOOK_SIM 行:", open_rows)
    for r in runs:
        print("\n[%s] now=%s phase=%s" % (r["label"], r["now_et"], r["phase"]))
        print("  closed=%s legs=%s alerts=%s" % (r["closed"], r["legs"] or "无", r["alerts"] or "无"))
        print("  豁免/影子打点=%s" % (r["exempt_or_shadow"] or "无"))
    print("\n══ 契约判定 (S0 复现 + S1/S3 归零 + S2 保留正常平仓) ══")
    verdicts = [
        ("S0 复现 P0 形态", len(runs[0]["legs"]) >= 1),
        ("S1 守卫拦下 (0 腿 0 告警)", runs[1]["legs"] == [] and runs[1]["alerts"] == []),
        ("S2 窗内仍平非豁免仓", len(runs[2]["legs"]) >= 1),
        ("S3 窗口外 0 腿", runs[3]["legs"] == []),
    ]
    for name, passed in verdicts:
        print(("  ✓ " if passed else "  ✗ ") + name)
    return 0 if all(p for _, p in verdicts) else 1


if __name__ == "__main__":
    sys.exit(main())