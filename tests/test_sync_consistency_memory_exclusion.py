"""一致性校验 Memory 分支的排除口径回归 (2026-10-20, RFC-151).

病灶 (10/19 实盘实测, 每 ~5min 一条贯穿全天):
  `_verify_and_sync_capital` 的 Memory 分支用
  `open_positions.owner_strategy='playbook_sim'` 构造排除集, 而 sync 循环
  (`_sync_to_position_store_locked`, L1701) 用 `playbook_open_check` —
  判据是 `trade_records.source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE')` 且未平。
  盘前/盘后 (EXT) 入场仓的 owner_strategy 写的是 `extended_hours_entry`
  (trade_records 仍记 PLAYBOOK_SIM), 排除集漏这一级 ⇒ 三个 EXT 仓
  (ASML/TSM/BA) 每轮被报成 "Memory 缺少 Broker 持仓", 而同一条 Done 行
  却印 "playbook自管: 4 | 一致性: ✓" — 两处口径漂移的现场对照。

本文件钉两个方向 (Gate 4 双向审计):
  1. playbook 自管仓 (含 EXT) 不进 Memory 比较 ⇒ 不报 (不得刷屏);
  2. 非 playbook 的 broker 仓不在 Memory ⇒ 仍必须报 (不得因修 1 而静默掉真缺口);
  3. 预载失败 (load 返回 None) 时回退逐 symbol 判定, 保留 fail-closed 语义。

回滚: revert app/core/position_sync.py 的 RFC-151 改动块 + 删本文件; 零运行时
行为影响 (本检查只发日志, 不参与交易决策)。
"""
import logging

import pytest

from app.core.position_sync import USPositionSync


class _RecLogger:
    """收集 logger.warning 的格式化结果 (真 logger 的 propagate=False 会丢 caplog)."""

    def __init__(self):
        self.warnings = []

    def warning(self, msg, *args, **kwargs):
        self.warnings.append(msg % args if args else msg)

    def info(self, msg, *args):
        pass

    def debug(self, msg, *args):
        pass

    def error(self, msg, *args):
        pass

    def critical(self, msg, *args):
        pass


class _FakeStore:
    def __init__(self, symbols):
        self._symbols = list(symbols)

    def all(self):
        out = []
        for s in self._symbols:
            p = type("P", (), {})()
            p.symbol = s
            p.size = 1
            p.direction = "LONG"
            out.append(p)
        return out


def _make_sync(monkeypatch, broker_syms, mem_syms):
    sync = USPositionSync(position_store=_FakeStore(mem_syms))
    sync._get_db_positions = lambda: {s: {"size": 1, "direction": "LONG"} for s in broker_syms}
    sync._broker_adapter = None  # 资金段直接 debug 返回, 本测试只关心一致性段
    sync._futu_ctx = None
    sync._last_reported_ghosts = frozenset()
    rec = _RecLogger()
    monkeypatch.setattr("app.core.position_sync.logger", rec)
    return sync, rec


def _futu(syms):
    return {s: {"qty": 1.0, "cost": 100.0, "direction": "LONG"} for s in syms}


def _consistency_lines(rec):
    return [w for w in rec.warnings if "US Sync Consistency" in w]


def test_ext_playbook_positions_not_reported_as_missing(monkeypatch):
    """EXT 入场仓 (owner_strategy=extended_hours_entry) 是 playbook 自管 ⇒ 不报缺失."""
    broker = {"US.ASML", "US.TSM", "US.BA"}
    sync, rec = _make_sync(monkeypatch, broker, mem_syms=set())

    # 真实口径: trade_records 里三个都是未平 PLAYBOOK_SIM
    monkeypatch.setattr(
        "app.playbook.pipeline.load_playbook_open_symbols", lambda: set(broker)
    )
    sync._verify_and_sync_capital(_futu(broker))

    assert not _consistency_lines(rec), (
        f"playbook 自管仓不应报 Memory 缺失, 实际: {_consistency_lines(rec)}"
    )


def test_non_playbook_broker_position_still_reported(monkeypatch):
    """非 playbook 仓不在 position_store ⇒ 必须报 (修 1 不得顺手静默真缺口)."""
    broker = {"US.ASML", "US.MSFT"}
    sync, rec = _make_sync(monkeypatch, broker, mem_syms=set())

    monkeypatch.setattr(
        "app.playbook.pipeline.load_playbook_open_symbols", lambda: {"US.ASML"}
    )
    sync._verify_and_sync_capital(_futu(broker))

    lines = _consistency_lines(rec)
    assert len(lines) == 1, lines
    assert "Memory 缺少 Broker 持仓" in lines[0]
    assert "US.MSFT" in lines[0]
    assert "US.ASML" not in lines[0], "playbook 仓仍被误报进缺失集合"


def test_preload_failure_falls_back_per_symbol(monkeypatch):
    """预载失败 (None) ⇒ 回退 is_playbook_open_position, 保留 fail-closed 语义."""
    broker = {"US.ASML", "US.MSFT"}
    sync, rec = _make_sync(monkeypatch, broker, mem_syms=set())

    monkeypatch.setattr(
        "app.playbook.pipeline.load_playbook_open_symbols", lambda: None
    )
    monkeypatch.setattr(
        "app.playbook.pipeline.is_playbook_open_position",
        lambda s: s == "US.ASML",
    )
    sync._verify_and_sync_capital(_futu(broker))

    lines = _consistency_lines(rec)
    assert len(lines) == 1, lines
    assert "US.MSFT" in lines[0] and "US.ASML" not in lines[0]


def test_consistency_check_no_owner_strategy_literal():
    """契约: 一致性校验不得再以 owner_strategy 字符串做 playbook 判定 (口径漂移根因)."""
    import inspect

    src = inspect.getsource(USPositionSync._verify_and_sync_capital)
    assert "owner_strategy = 'playbook_sim'" not in src
    assert "load_playbook_open_symbols" in src


def test_check_exception_is_not_silent(monkeypatch):
    """校验段异常必须 WARNING 可见 — 否则「误报消失」与「整段死了」在日志上无法区分."""
    sync = USPositionSync(position_store=_FakeStore([]))
    sync._broker_adapter = None
    sync._futu_ctx = None
    rec = _RecLogger()
    monkeypatch.setattr("app.core.position_sync.logger", rec)

    def _boom():
        raise RuntimeError("db locked (synthetic)")

    sync._get_db_positions = _boom
    sync._verify_and_sync_capital({})  # 不得抛出

    lines = [w for w in rec.warnings if "校验异常" in w]
    assert lines, f"整段校验异常必须 WARNING 暴露, 实际 warnings={rec.warnings}"
    assert "db locked" in lines[0]