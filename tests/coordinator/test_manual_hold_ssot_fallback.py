"""ManualHoldRegistry 自动交易闸门 SSOT 回落回归测试 (2026-08-28, 全链路复查 P1-3).

背景: 该注册表是**进程内单例**, 而手动单由面板进程 (ftseek-dashboard.service) 下:
web_dashboard/order/executor.py:222 的 register() 只写进面板进程自己的 dict, 主交易
进程 (闸门读方: playbook_service:1090 / position_monitor:979 / orb/engine:600,870)
当日永远看不见 → 自动管线可与用户手动仓同标的对打, 只有重启才 sync。
(同日生产实证: open_positions 有 US.MU owner=manual, 主进程注册表为空。)

修复 = is_auto_blocked 内存未命中时回落 open_positions (SSOT), 60s TTL 缓存。

覆盖点:
  1. 跨进程场景: 内存空 + DB owner=manual(同账户) → 拦 (修复前放行)
  2. 账户隔离不变: real 手动仓不拦 sim 自动交易; AUTO_TRADE_ACCOUNT=real 时拦
  3. account_type 取值映射: simulate 与 sim 两种写法都认
  4. owner=auto 行不拦
  5. 内存命中仍是首选 (不等 TTL)
  6. 空结果也被缓存 (每 60s 一次查询, 不随仓位轮询打库)
  7. 查询失败 → 不拦 (维持原 fail-open 语义) 但 ERROR+degradation_monitor 留痕 (2026-08-28 升级)
  8. is_manual_held 纯内存语义未被改动 (面板/预检读方口径不变)

回滚: 删本文件 + 还原 manual_hold_registry.is_auto_blocked 为单行 return 即可。
"""
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.coordinator.manual_hold_registry import ManualHoldRegistry, manual_hold_registry


@pytest.fixture
def ssot_env(tmp_path, monkeypatch):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE open_positions ("
        "symbol TEXT PRIMARY KEY, owner TEXT, account_type TEXT, size REAL)"
    )
    conn.commit()
    conn.close()

    import app.core.paths as paths_mod
    monkeypatch.setattr(paths_mod, "get_trade_records_db", lambda: db)
    # RFC-131 (2026-10-03, 153bd8a42): _ssot_manual_symbols 改用
    # get_trade_records_db_for_arm(account), real 臂直接返回
    # ftseek_us_live.db 绕过 get_trade_records_db. 测试须同时桩两者,
    # 否则 AUTO_TRADE_ACCOUNT=real 用例读不到 tmp DB 里的测试数据.
    monkeypatch.setattr(paths_mod, "get_trade_records_db_for_arm",
                        lambda target_env: db)

    # 进程级类属性: 每用例复位 (缓存 + 内存注册表)
    monkeypatch.setattr(ManualHoldRegistry, "_ssot_cache",
                        {"ts": 0.0, "account": "", "symbols": set()})
    saved = dict(manual_hold_registry._holdings)
    manual_hold_registry._holdings.clear()
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "sim")
    yield db
    manual_hold_registry._holdings.clear()
    manual_hold_registry._holdings.update(saved)


def _set_positions(db, rows):
    conn = sqlite3.connect(str(db))
    conn.execute("DELETE FROM open_positions")
    conn.executemany(
        "INSERT INTO open_positions (symbol, owner, account_type, size) "
        "VALUES (?, ?, ?, 10)", rows,
    )
    conn.commit()
    conn.close()


# ── 1. 跨进程失明 (主缺陷) ──
def test_dashboard_manual_hold_blocks_auto_in_main_process(ssot_env):
    """面板进程下的手动单: 主进程内存 dict 为空, 但 SSOT 有行 → 必须拦."""
    _set_positions(ssot_env, [("US.MU", "manual", "simulate")])
    assert manual_hold_registry.is_manual_held("US.MU") is False   # 内存仍瞎 (现状)
    assert manual_hold_registry.is_auto_blocked("US.MU") is True   # 闸门不再瞎


def test_without_fallback_gate_would_have_passed(ssot_env):
    """对照留痕: 只看内存的旧语义对同一数据集放行."""
    _set_positions(ssot_env, [("US.MU", "manual", "simulate")])
    assert manual_hold_registry.is_manual_held("US.MU", account="sim") is False


# ── 2. 账户隔离语义不变 ──
def test_real_manual_does_not_block_sim_auto(ssot_env):
    _set_positions(ssot_env, [("US.MU", "manual", "real")])
    assert manual_hold_registry.is_auto_blocked("US.MU") is False


def test_real_manual_blocks_auto_when_env_is_real(ssot_env, monkeypatch):
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "real")
    _set_positions(ssot_env, [("US.MU", "manual", "real")])
    # 换账户 → 缓存必须失效重查 (account 参与缓存键)
    assert manual_hold_registry.is_auto_blocked("US.MU") is True


def test_sim_auto_not_blocked_by_manual_on_other_symbol(ssot_env):
    _set_positions(ssot_env, [("US.AVGO", "manual", "simulate")])
    assert manual_hold_registry.is_auto_blocked("US.MU") is False


# ── 3. account_type 取值映射 ──
@pytest.mark.parametrize("acc_type", ["simulate", "sim"])
def test_sim_account_aliases(ssot_env, acc_type):
    _set_positions(ssot_env, [("US.MU", "manual", acc_type)])
    assert manual_hold_registry.is_auto_blocked("US.MU") is True


# ── 4. auto 行不拦 ──
def test_auto_owned_rows_never_block(ssot_env):
    _set_positions(ssot_env, [("US.AVGO", "auto", "simulate"),
                              ("US.PDD", "auto", "simulate")])
    assert manual_hold_registry.is_auto_blocked("US.AVGO") is False
    assert manual_hold_registry.is_auto_blocked("US.PDD") is False


# ── 5. 内存命中优先 ──
def test_memory_hit_short_circuits_query(ssot_env):
    _set_positions(ssot_env, [])
    manual_hold_registry.register("US.MU", "LONG", 10, "manual trade", account="sim")
    assert manual_hold_registry.is_auto_blocked("US.MU") is True


# ── 6. 空结果也缓存 (不把闸门打成每轮查库) ──
def test_empty_result_is_cached_for_ttl(ssot_env, monkeypatch):
    import time as _t
    _set_positions(ssot_env, [])
    assert manual_hold_registry.is_auto_blocked("US.MU") is False
    # 缓存写入后改库 → TTL 内仍用缓存值
    _set_positions(ssot_env, [("US.MU", "manual", "simulate")])
    assert manual_hold_registry.is_auto_blocked("US.MU") is False
    # 手动令缓存过期 → 立刻看到新行
    monkeypatch.setitem(ManualHoldRegistry._ssot_cache, "ts", _t.time() - 999)
    assert manual_hold_registry.is_auto_blocked("US.MU") is True


# ── 7. 查询失败不拦但可见 ──
def test_query_failure_fails_open_with_warning(tmp_path, monkeypatch, caplog):
    empty = tmp_path / "no_open_positions.db"      # 有库无表 → OperationalError
    import app.core.paths as paths_mod
    monkeypatch.setattr(paths_mod, "get_trade_records_db", lambda: empty)
    monkeypatch.setattr(ManualHoldRegistry, "_ssot_cache",
                        {"ts": 0.0, "account": "", "symbols": set()})
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "sim")

    # 2026-08-28 升级: warning → ERROR + degradation_monitor 记录 (盲区可见性,
    #   pre_trade_gate fail-open 可见性同款): 协调门保持 fail-open 不变.
    _recorded = []
    import app.core.degradation_monitor as _dm
    monkeypatch.setattr(_dm, "get_degradation_monitor",
                        lambda: type("M", (), {"record": staticmethod(
                            lambda mod, msg="": _recorded.append((mod, msg)))})())
    import logging
    with caplog.at_level(logging.ERROR):
        assert manual_hold_registry.is_auto_blocked("US.MU") is False
    assert "回落查询失败" in caplog.text
    assert "fail-open" in caplog.text, "盲区性质 (fail-open) 必须在 ERROR 里言明"
    assert _recorded and _recorded[0][0] == "manual_hold_ssot_fallback", \
        f"degradation_monitor 未记录: {_recorded}"
