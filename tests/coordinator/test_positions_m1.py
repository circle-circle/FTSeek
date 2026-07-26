"""M1 持仓多源集成测试 (2026-05-16)"""
import os
import sys
import tempfile
from datetime import datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from app.positions import (
    PositionSource, ProtectionState,
    get_position_source, get_protection_state, tag_position,
    is_strategy_managed, is_protected,
    StartupReconciler,
)
from app.positions.protection import ProtectionController


# ─── Mock Position ───
class MockPosition:
    def __init__(self, symbol="NVDA", direction="LONG", size=100, price=145.0,
                 owner_strategy=None, strategy_id=None, metadata=None):
        self.symbol = symbol
        self.direction = direction
        self.size = size
        self.avg_entry_price = price
        self.owner_strategy = owner_strategy
        self.strategy_id = strategy_id or ""
        self.metadata = metadata or {}


class MockPositionStore:
    def __init__(self, positions=None):
        self._positions = {p.symbol: p for p in (positions or [])}
        import threading
        self._lock = threading.Lock()

    def all(self):
        return list(self._positions.values())

    def get(self, symbol):
        return self._positions.get(symbol)

    def add(self, symbol, position):
        self._positions[symbol] = position

    def has(self, symbol):
        return symbol in self._positions


# ─── PositionSource enum ───
def test_source_from_str_main():
    assert PositionSource.from_str("main") == PositionSource.AUTO_MAIN
    assert PositionSource.from_str("voting_llm") == PositionSource.AUTO_MAIN


def test_source_from_str_retired_scalping():
    """scalping 策略已退役 (AUTO_SCALPING 枚举删除) — 历史字符串必须落 UNKNOWN 兜底"""
    assert PositionSource.from_str("scalping") == PositionSource.UNKNOWN


def test_source_from_str_manual():
    assert PositionSource.from_str("manual") == PositionSource.MANUAL
    assert PositionSource.from_str("futu_manual") == PositionSource.MANUAL
    # P0-fix (2026-06-15): futu_sync 是同步标记非来源, 来源不明 → UNKNOWN (配 PROTECTED)
    assert PositionSource.from_str("futu_sync") == PositionSource.UNKNOWN


def test_source_from_str_legacy():
    assert PositionSource.from_str("broker_recovery") == PositionSource.LEGACY


def test_source_from_str_unknown():
    assert PositionSource.from_str("garbage") == PositionSource.UNKNOWN


def test_source_is_auto():
    assert PositionSource.AUTO_MAIN.is_auto() is True
    assert PositionSource.MANUAL.is_auto() is False


# ─── ProtectionState 默认 ───
def test_default_protection_for_auto():
    assert ProtectionState.default_for(PositionSource.AUTO_MAIN) == ProtectionState.MANAGED


def test_default_protection_for_manual():
    assert ProtectionState.default_for(PositionSource.MANUAL) == ProtectionState.PROTECTED


def test_default_protection_for_legacy():
    assert ProtectionState.default_for(PositionSource.LEGACY) == ProtectionState.PROTECTED


# ─── tag_position ───
def test_tag_position_auto_main():
    p = MockPosition()
    tag_position(p, PositionSource.AUTO_MAIN)
    assert p.metadata["source"] == "auto_main"
    assert p.metadata["protection"] == "managed"


def test_tag_position_legacy_protected():
    p = MockPosition()
    tag_position(p, PositionSource.LEGACY)
    assert p.metadata["source"] == "legacy"
    assert p.metadata["protection"] == "protected"


# ─── get_position_source 优先级 ───
def test_get_source_metadata_priority():
    p = MockPosition(metadata={"source": "manual"}, owner_strategy="main")
    # metadata 优先
    assert get_position_source(p) == PositionSource.MANUAL


def test_get_source_owner_fallback():
    # scalping 已退役 → 改用在役策略 fast_lane 验证 owner_strategy 回退路径
    p = MockPosition(owner_strategy="fast_lane", metadata={})
    assert get_position_source(p) == PositionSource.AUTO_FAST_LANE
    # 退役策略名走 owner 回退时必须落 UNKNOWN (不许误归 auto)
    p2 = MockPosition(owner_strategy="scalping", metadata={})
    assert get_position_source(p2) == PositionSource.UNKNOWN


def test_get_source_strategy_id_last():
    p = MockPosition(strategy_id="voting_llm")
    assert get_position_source(p) == PositionSource.AUTO_MAIN


# ─── is_strategy_managed / is_protected ───
def test_managed_when_auto():
    p = MockPosition(owner_strategy="main")
    assert is_strategy_managed(p) is True
    assert is_protected(p) is False


def test_protected_when_manual():
    p = MockPosition(metadata={"source": "manual"})
    assert is_strategy_managed(p) is False
    assert is_protected(p) is True


def test_frozen_explicit():
    p = MockPosition(metadata={"source": "auto_main", "protection": "frozen"})
    assert is_strategy_managed(p) is False
    assert is_protected(p) is True


# ─── ProtectionController ───
@pytest.fixture
def controller_with_mock_store():
    p_manual = MockPosition(symbol="NVDA", metadata={"source": "manual"})
    p_main = MockPosition(symbol="ARM", owner_strategy="main")
    store = MockPositionStore([p_manual, p_main])

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db = f.name
    ctrl = ProtectionController(position_store=store, audit_db_path=db)
    yield ctrl, store, db
    os.unlink(db)


def test_takeover_manual(controller_with_mock_store):
    ctrl, store, _ = controller_with_mock_store
    assert ctrl.takeover("NVDA", "用户决定接管") is True
    p = store.get("NVDA")
    assert get_protection_state(p) == ProtectionState.MANAGED


def test_release_main_to_protected(controller_with_mock_store):
    ctrl, store, _ = controller_with_mock_store
    assert ctrl.release("ARM", "停止策略") is True
    p = store.get("ARM")
    assert get_protection_state(p) == ProtectionState.PROTECTED


def test_freeze(controller_with_mock_store):
    ctrl, store, _ = controller_with_mock_store
    assert ctrl.freeze("NVDA", "财报前冻结") is True
    p = store.get("NVDA")
    assert get_protection_state(p) == ProtectionState.FROZEN


def test_takeover_unknown_symbol(controller_with_mock_store):
    ctrl, _, _ = controller_with_mock_store
    assert ctrl.takeover("NONEXISTENT") is False


def test_list_protected(controller_with_mock_store):
    ctrl, _, _ = controller_with_mock_store
    plist = ctrl.list_protected()
    syms = [p["symbol"] for p in plist]
    assert "NVDA" in syms       # manual = protected
    assert "ARM" not in syms    # main = managed


# ─── StartupReconciler ───
def test_reconciler_broker_only_added():
    """broker 有, 本地无 → 自动补录, 标 LEGACY + PROTECTED"""
    store = MockPositionStore([])
    rec = StartupReconciler(store)
    # 直接调内部
    rec._add_legacy_position("NVDA", {"qty": 100, "cost": 145.50, "direction": "LONG"})
    p = store.get("NVDA")
    assert p is not None
    assert get_position_source(p) == PositionSource.LEGACY
    assert get_protection_state(p) == ProtectionState.PROTECTED


def test_reconciler_matched_tags_unknown_as_legacy():
    """matched 但 source UNKNOWN → 补 LEGACY 标"""
    p = MockPosition(symbol="NVDA")  # 无 owner/strategy
    store = MockPositionStore([p])
    rec = StartupReconciler(store)
    result = rec._reconcile(
        broker_positions={"NVDA": {"qty": 100, "cost": 145, "direction": "LONG"}},
        local_positions={"NVDA": p},
    )
    assert result.matched == 1
    assert result.tagged_legacy == 1
    assert get_position_source(p) == PositionSource.LEGACY


def test_reconciler_local_only_marked_ghost():
    """本地有, broker 无 → 标 ghost"""
    p = MockPosition(symbol="DEAD")
    store = MockPositionStore([p])
    rec = StartupReconciler(store)
    result = rec._reconcile(broker_positions={}, local_positions={"DEAD": p})
    assert result.local_only_ghosted == 1
    assert p.metadata.get("is_ghost") is True


# ─── M1.1 智能 source 推断 ───
def _make_reconciler_with_db(rows_trade_records=None, rows_open_positions=None):
    """构造 reconciler + 临时 DB 灌入 trade_records / open_positions"""
    import sqlite3
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT, symbol TEXT, direction TEXT,
            entry_time TEXT, exit_time TEXT,
            owner_strategy TEXT, source_type TEXT, strategy_id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE open_positions (
            symbol TEXT PRIMARY KEY, action_log_json TEXT
        )
    """)
    for r in (rows_trade_records or []):
        conn.execute(
            "INSERT INTO trade_records (symbol, owner_strategy, source_type, strategy_id, entry_time) VALUES (?,?,?,?,?)",
            r
        )
    for r in (rows_open_positions or []):
        conn.execute("INSERT INTO open_positions (symbol, action_log_json) VALUES (?,?)", r)
    conn.commit()
    conn.close()

    rec = StartupReconciler(MockPositionStore([]), db_path=db)
    return rec, db


def test_infer_source_from_trade_records_main():
    """trade_records 有入场记录 → 识别为 AUTO_MAIN + MANAGED"""
    rec, db = _make_reconciler_with_db(
        rows_trade_records=[("US.QQQ", "main", "MAIN_PIPELINE", "voting_llm", "2026-05-03T09:00:00")]
    )
    p = MockPosition(symbol="US.QQQ", strategy_id="futu_sync")  # metadata 丢失
    src, prot = rec._infer_source("US.QQQ", p)
    assert src == PositionSource.AUTO_MAIN
    assert prot == ProtectionState.MANAGED  # 策略孤儿应 MANAGED 续命
    os.unlink(db)


def test_infer_source_from_action_log():
    """trade_records 空, 但 action_log_json 含 update_trail → AUTO_MAIN MANAGED"""
    rec, db = _make_reconciler_with_db(
        rows_open_positions=[("US.QQQ", '[{"action":"update_trail","source":"exit_monitor"}]')]
    )
    p = MockPosition(symbol="US.QQQ", strategy_id="futu_sync")
    src, prot = rec._infer_source("US.QQQ", p)
    assert src == PositionSource.AUTO_MAIN
    assert prot == ProtectionState.MANAGED
    os.unlink(db)


def test_infer_source_legacy_fallback():
    """trade_records + action_log 全空 + 无 strategy → LEGACY + PROTECTED"""
    rec, db = _make_reconciler_with_db()
    p = MockPosition(symbol="US.UNKNOWN", strategy_id="", owner_strategy="")
    src, prot = rec._infer_source("US.UNKNOWN", p)
    assert src == PositionSource.LEGACY
    assert prot == ProtectionState.PROTECTED
    os.unlink(db)


def test_infer_source_strategy_id_heuristic():
    """trade_records 空, 但 strategy_id='fast_lane' → AUTO_FAST_LANE MANAGED.

    scalping 已退役: strategy_id='scalping' 不再识别为 auto, 落 LEGACY+PROTECTED 兜底.
    """
    rec, db = _make_reconciler_with_db()
    p = MockPosition(symbol="US.X", strategy_id="fast_lane")
    src, prot = rec._infer_source("US.X", p)
    assert src == PositionSource.AUTO_FAST_LANE
    assert prot == ProtectionState.MANAGED
    # 退役策略名 → 兜底 LEGACY + PROTECTED (不许误收养为 auto)
    p2 = MockPosition(symbol="US.X", strategy_id="scalping", owner_strategy="")
    src2, prot2 = rec._infer_source("US.X", p2)
    assert src2 == PositionSource.LEGACY
    assert prot2 == ProtectionState.PROTECTED
    os.unlink(db)


def test_infer_source_skip_futu_sync():
    """strategy_id='futu_sync' 不应被当作策略名 (是同步标记)"""
    rec, db = _make_reconciler_with_db()
    p = MockPosition(symbol="US.X", strategy_id="futu_sync", owner_strategy="")
    src, prot = rec._infer_source("US.X", p)
    # futu_sync 跳过 strategy_id 启发式 → fallback LEGACY
    assert src == PositionSource.LEGACY
    assert prot == ProtectionState.PROTECTED
    os.unlink(db)


def test_qqq_real_scenario_adopted():
    """5/16 实际 QQQ 场景: trade_records 0 行 + action_log_json 含 ExitMonitor 痕迹"""
    rec, db = _make_reconciler_with_db(
        rows_trade_records=[],  # 0 行 — 入场记录丢失
        rows_open_positions=[
            ("US.QQQ",
             '[{"time":"2026-05-14T00:22:06","action":"update_trail","source":"exit_monitor","trace_id":""}]')
        ]
    )
    p = MockPosition(symbol="US.QQQ", strategy_id="futu_sync", owner_strategy="main",
                     metadata={})  # 空 metadata, 模拟 reconcile 前
    src, prot = rec._infer_source("US.QQQ", p)
    # 期望: 识别为策略仓 (因为 action_log 显示 ExitMonitor 在管)
    assert src == PositionSource.AUTO_MAIN
    assert prot == ProtectionState.MANAGED
    os.unlink(db)
