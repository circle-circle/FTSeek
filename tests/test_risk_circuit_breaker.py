"""RiskCircuitBreaker 单测 — TZ-1 (DEB-20260830-timezone-duplication-audit)。

病根 (2026-08-30 实测, 修前): ① 停止窗口判定/赋值用服务器 CST date.today()
(CST 午夜 = ET 正午, "停开仓1日"窗口盘中翻日); ② _stop_until 纯内存字段, 唯一调用方
(pipeline 周报) 每轮新建实例 → 连亏5锁存从未跨调用生效 (窗口从未真正存在过)。
修: ET 日历日 + playbook_risk_state 落盘。预注册判据全四条对应本文件前四个用例:
  ① 连亏5 → _stop_until 落盘非空
  ② 新实例 (跨调用/重启) → 窗口仍在, 且实时条件已消失仍拦 (锁存语义本体)
  ③ ET 午夜边界: ET 23:30 窗口未过期 (旧 CST 口径在 ET 正午就提前放行)
  ④ 生产零行为变化: 减仓/日亏路径语义回归钉 (原行为不变的证明)
"""
import sqlite3
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.playbook.db import PlaybookDB
from app.playbook.risk_circuit_breaker import RiskCircuitBreaker

ET = ZoneInfo("America/New_York")


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    pdb = PlaybookDB(conn=conn)
    pdb.ensure_tables()
    yield pdb
    conn.close()


def _now_et(dt_str: str):
    """固定 ET 时间 (aware), patch 进熔断器模块。"""
    return datetime.fromisoformat(dt_str).replace(tzinfo=ET)


def _add_executed(db, symbol, outcome, execution_time, pnl, trade_date="2026-08-28"):
    inst_id = db.save_instruction({
        "date": trade_date, "symbol": symbol, "direction": "BUY",
        "entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
        "position_size": 50, "confidence": 72, "risk_amount": 250.0,
        "risk_reward": 2.0, "conditions": ["above_ma20"],
    })
    db.update_instruction_tracking(inst_id, {
        "executed": 1, "outcome": outcome, "pnl": pnl,
        "execution_price": 100.0, "execution_time": execution_time,
    })
    return inst_id


# ── 判据①: 连亏5 → 熔断 + 落盘非空 ──────────────────────────────
def test_consecutive_5_arms_and_persists(db):
    for i in range(5):
        _add_executed(db, f"US.L{i}", "loss", f"2026-08-28T1{i}:00:00", -100.0)

    with patch("app.playbook.risk_circuit_breaker.now_et",
               return_value=_now_et("2026-08-28T15:00:00")):
        st = RiskCircuitBreaker(db).check_status(100000)

    assert st["consecutive_losses"] == 5
    assert st["position_multiplier"] == 0.0
    assert st["trading_allowed"] is False
    assert db.get_risk_state("stop_until") == "2026-08-29"  # 8/28 + 1日


# ── 判据②: 新实例窗口仍在 (锁存跨调用/重启; 实时条件消失仍拦) ──────
def test_latch_survives_new_instance_after_condition_clears(db):
    for i in range(5):
        _add_executed(db, f"US.L{i}", "loss", f"2026-08-28T1{i}:00:00", -100.0)
    with patch("app.playbook.risk_circuit_breaker.now_et",
               return_value=_now_et("2026-08-28T15:00:00")):
        RiskCircuitBreaker(db).check_status(100000)

    # 锁存防的正是这种: 条件刚缓解 (最近一笔已盈利) 就立刻又开仓
    _add_executed(db, "US.W1", "win", "2026-08-28T16:00:00", 200.0)

    with patch("app.playbook.risk_circuit_breaker.now_et",
               return_value=_now_et("2026-08-28T20:00:00")):
        st2 = RiskCircuitBreaker(db).check_status(100000)  # 新实例 = 下轮周报/重启

    assert st2["consecutive_losses"] == 0            # 实时条件已消失
    assert st2["trading_allowed"] is False           # 锁存仍在 → 停满1日
    assert "连续亏损熔断中" in st2["stop_reason"]


# ── 判据③: ET 午夜边界 (服务器 CST 口径在 ET 正午提前翻日) ─────────
def test_et_midnight_boundary_window_judged_in_et(db):
    for i in range(5):
        _add_executed(db, f"US.L{i}", "loss", f"2026-08-28T1{i}:00:00", -100.0)
    with patch("app.playbook.risk_circuit_breaker.now_et",
               return_value=_now_et("2026-08-28T15:00:00")):
        RiskCircuitBreaker(db).check_status(100000)
    _add_executed(db, "US.W1", "win", "2026-08-28T16:00:00", 200.0)

    # ET 8/28 23:30 = CST 8/29 11:30: 旧 date.today()(CST)=8/29 会判窗口过期提前放行;
    # ET 口径 8/28 < 8/29 → 仍停
    with patch("app.playbook.risk_circuit_breaker.now_et",
               return_value=_now_et("2026-08-28T23:30:00")):
        st_late = RiskCircuitBreaker(db).check_status(100000)
    assert st_late["trading_allowed"] is False

    # ET 8/29 00:30: 窗口到期 → 放行 (锁存不无限延长)
    with patch("app.playbook.risk_circuit_breaker.now_et",
               return_value=_now_et("2026-08-29T00:30:00")):
        st_next = RiskCircuitBreaker(db).check_status(100000)
    assert st_next["trading_allowed"] is True


# ── 判据④: 原有语义回归钉 (生产零行为变化的前提) ─────────────────
def test_reduce_at_3_consecutive_no_latch(db):
    for i in range(3):
        _add_executed(db, f"US.L{i}", "loss", f"2026-08-28T1{i}:00:00", -100.0)

    with patch("app.playbook.risk_circuit_breaker.now_et",
               return_value=_now_et("2026-08-28T15:00:00")):
        st = RiskCircuitBreaker(db).check_status(100000)

    assert st["consecutive_losses"] == 3
    assert st["position_multiplier"] == 0.5   # 减半, 未到停开仓
    assert st["trading_allowed"] is True
    assert db.get_risk_state("stop_until") is None  # 未触发停开仓 → 不落盘


def test_daily_loss_path_unchanged(db):
    _add_executed(db, "US.BIG", "loss", "2026-08-28T10:00:00", -2000.0)

    with patch("app.playbook.risk_circuit_breaker.now_et",
               return_value=_now_et("2026-08-28T15:00:00")):
        st = RiskCircuitBreaker(db).check_status(100000)

    assert st["daily_loss_ok"] is False
    assert st["daily_loss"] == -2000.0
    assert st["trading_allowed"] is False
    assert st["stop_reason"] == "日内亏损超限"
    assert db.get_risk_state("stop_until") is None  # 日亏不武装连亏锁存 (原语义)


def test_reset_clears_persisted_latch(db):
    for i in range(5):
        _add_executed(db, f"US.L{i}", "loss", f"2026-08-28T1{i}:00:00", -100.0)
    breaker = RiskCircuitBreaker(db)
    with patch("app.playbook.risk_circuit_breaker.now_et",
               return_value=_now_et("2026-08-28T15:00:00")):
        breaker.check_status(100000)
    assert db.get_risk_state("stop_until") == "2026-08-29"

    breaker.reset()
    assert db.get_risk_state("stop_until") is None
    assert RiskCircuitBreaker(db)._stop_until is None  # 新实例按未熔断恢复


def test_unparseable_persisted_value_ignored(db):
    db.set_risk_state("stop_until", "not-a-date")
    breaker = RiskCircuitBreaker(db)
    assert breaker._stop_until is None  # 脏值按未熔断处理, 不炸
