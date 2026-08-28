"""AbsoluteCircuitBreaker 启动重建回归测试 (2026-08-28, 全链路复查 P0-2).

背景: 本类自称"最后防线, 不受任何 feature flag 控制", 但状态纯内存 —— 而
ftseek-us.service 配 Restart=on-failure / RestartSec=30, 盘中一次崩溃自愈就把
"当日已触发停新仓"清零且零告警 (即一次重启即可解除最后防线)。
修复 = __init__ 末尾 _restore_from_db(), 从 trade_records 按出场日 + 净额重建
日亏/时亏/连亏 (喂数方唯一: playbook/pipeline._close_trade_record)。

覆盖点:
  1. 日亏超限 → 重启后仍停新仓, 且 _trade_date 已对齐今日 (不被 _check_new_day 抹掉)
  2. 未超限 → 放行 (重建不是"一律拦")
  3. 范围: 只计 PLAYBOOK_SIM/PLAYBOOK_LIVE (与唯一喂数方一致)
  4. 净额口径: 毛值不触发而净额触发 (与 8/28 喂数侧改净一致)
  5. pnl_net NULL 旧行回退毛额
  6. 未了结行不计
  7. 连亏链按 exit_time 时序, 中途盈利行清零
  8. 小时窗: 本小时净额超限 → 暂停至下一小时
  9. 前一 ET 日行不计入
 10. 重建失败 (无表/无库) → 不炸启动, 保留零值, 但 WARNING 可见

回滚: 删本文件 + 移除 absolute_circuit_breaker._restore_from_db 调用即可。
"""
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.risk.absolute_circuit_breaker import AbsoluteCircuitBreaker
from app.utils.timezone import _et_trade_date, now_et


@pytest.fixture
def acb_env(tmp_path, monkeypatch):
    """独立台账库 + 单例复位 (避污染其他用例)."""
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records ("
        "id INTEGER PRIMARY KEY, symbol TEXT, trade_date TEXT, exit_time TEXT, "
        "exit_price REAL, pnl_net REAL, realized_pnl REAL, source_type TEXT)"
    )
    conn.commit()
    conn.close()

    import app.core.paths as paths_mod
    monkeypatch.setattr(paths_mod, "get_trade_records_db", lambda: db)
    monkeypatch.setattr(AbsoluteCircuitBreaker, "_instance", None)
    return db


def _today():
    return _et_trade_date(now_et())


def _hour_prefix():
    return now_et().strftime("%Y-%m-%dT%H")


def _rows(db, spec, source_type="PLAYBOOK_SIM", day=None):
    """spec: [(net, gross|None), ...] 按时间序落同一 ET 日/本小时."""
    day = day or _today()
    conn = sqlite3.connect(str(db))
    for i, (net, gross) in enumerate(spec):
        conn.execute(
            "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
            "pnl_net, realized_pnl, source_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("US.MU", day, f"{day}T{9 + i % 6:02d}:15:00-04:00", 100.0,
             net, gross, source_type),
        )
    conn.commit()
    conn.close()


def _rows_hour(db, spec):
    """本小时内的行 (exit_time 前 13 位 = now_et 小时前缀)."""
    conn = sqlite3.connect(str(db))
    for i, (net, gross) in enumerate(spec):
        conn.execute(
            "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
            "pnl_net, realized_pnl, source_type) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("US.MU", _today(), f"{_hour_prefix()}:{i * 5 + 1:02d}:00-04:00",
             100.0, net, gross, "PLAYBOOK_SIM"),
        )
    conn.commit()
    conn.close()


# ── 1. 日亏超限跨重启仍生效 ──
def test_daily_trip_survives_restart(acb_env):
    _rows(acb_env, [(-2100.0, -2090.0)])
    b = AbsoluteCircuitBreaker()
    allowed, reason = b.can_open_new_position("playbook")
    assert allowed is False
    assert "日亏损" in reason
    # 再查一次: _trade_date 已对齐今日 → 不会被"新交易日"重置抹掉
    assert b.can_open_new_position("playbook")[0] is False
    assert b._trade_date == _today()


def test_daily_pnl_value_restored_exactly(acb_env):
    _rows(acb_env, [(-800.0, -795.0), (-450.0, -448.0)])
    b = AbsoluteCircuitBreaker()
    assert b._daily_pnl == pytest.approx(-1250.0)
    assert b.can_open_new_position("playbook")[0] is True


# ── 2. 重建范围与唯一喂数方一致 ──
def test_non_playbook_sources_not_counted(acb_env):
    """ACB 只被 playbook 喂过 (record_trade_result 唯一调用点), 故恢复范围同口径.
    新增喂数策略时必须同步扩 _src_filter, 否则恢复值低于实时累计值。"""
    _rows(acb_env, [(-5000.0, -4990.0)], source_type="MAIN_PIPELINE")
    b = AbsoluteCircuitBreaker()
    assert b._daily_pnl == 0.0
    assert b.can_open_new_position("playbook")[0] is True


# ── 3. 净额口径 (与 8/28 喂数侧改净一致) ──
def test_net_caliber_trips_where_gross_would_not(acb_env):
    """毛 -1990 不触发, 净 -2010 触发 — 与 record_trade_result 现喂净额同尺."""
    _rows(acb_env, [(-2010.0, -1990.0)])
    b = AbsoluteCircuitBreaker()
    assert b._daily_pnl == pytest.approx(-2010.0)
    assert b.can_open_new_position("playbook")[0] is False


def test_legacy_row_without_net_falls_back_to_gross(acb_env):
    _rows(acb_env, [(None, -2200.0)])
    b = AbsoluteCircuitBreaker()
    assert b._daily_pnl == pytest.approx(-2200.0)
    assert b._daily_paused is True


# ── 4. 只算已了结 ──
def test_unclosed_rows_excluded(acb_env):
    conn = sqlite3.connect(str(acb_env))
    conn.execute(
        "INSERT INTO trade_records (symbol, trade_date, exit_time, exit_price, "
        "pnl_net, realized_pnl, source_type) VALUES (?, ?, ?, 0, -9000.0, -9000.0, "
        "'PLAYBOOK_SIM')",
        ("US.MU", _today(), f"{_today()}T10:00:00-04:00"),
    )
    conn.commit()
    conn.close()
    b = AbsoluteCircuitBreaker()
    assert b._daily_pnl == 0.0


# ── 5. 连亏链 ──
def test_five_consecutive_losses_pause_strategy(acb_env):
    _rows(acb_env, [(-100.0, -99.0)] * 5)
    b = AbsoluteCircuitBreaker()
    assert b._consecutive_losses["playbook"] == 5
    allowed, reason = b.can_open_new_position("playbook")
    assert allowed is False
    assert "连亏" in reason


def test_win_breaks_consecutive_chain(acb_env):
    """时序: 亏亏亏亏 → 盈 → 亏 ⇒ 尾部连亏 1 笔 (不是 5)。"""
    _rows(acb_env, [(-100.0, -99.0)] * 4 + [(50.0, 51.0), (-100.0, -99.0)])
    b = AbsoluteCircuitBreaker()
    assert b._consecutive_losses["playbook"] == 1
    assert b.can_open_new_position("playbook")[0] is True


# ── 6. 小时窗 ──
def test_hourly_trip_restored(acb_env):
    _rows_hour(acb_env, [(-1100.0, -1090.0)])
    b = AbsoluteCircuitBreaker()
    allowed, reason = b.can_open_new_position("playbook")
    assert allowed is False
    assert "小时亏损" in reason
    assert b._hourly_paused_until is not None


# ── 7. 前一 ET 日不计 ──
def test_previous_et_day_not_counted(acb_env):
    from datetime import timedelta
    prev = _et_trade_date(now_et() - timedelta(days=1))
    _rows(acb_env, [(-3000.0, -2990.0)], day=prev)
    b = AbsoluteCircuitBreaker()
    assert b._daily_pnl == 0.0
    assert b._trade_date == _today()
    assert b.can_open_new_position("playbook")[0] is True


# ── 8. 重建失败: 不炸启动但必须留痕 ──
def test_restore_failure_keeps_zero_state_and_warns(tmp_path, monkeypatch, caplog):
    missing = tmp_path / "no_such.db"   # 库都不存在
    import app.core.paths as paths_mod
    monkeypatch.setattr(paths_mod, "get_trade_records_db", lambda: missing)
    monkeypatch.setattr(AbsoluteCircuitBreaker, "_instance", None)

    import logging
    with caplog.at_level(logging.WARNING):
        b = AbsoluteCircuitBreaker()          # 不得抛
    assert b._daily_pnl == 0.0
    assert "重建失败" in caplog.text
