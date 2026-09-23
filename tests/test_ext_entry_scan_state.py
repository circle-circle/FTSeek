#!/usr/bin/env python3
"""RFC-101 L2 (scripts/ext_entry_scan_state.py) 的判据钉。

覆盖: 开关关闭零副作用 / 命中 / 到期 / 跳变失效 / 类别表外不缓存 / 跨日失效 /
      损坏 fail-open / 影子只记账 / 观测行 / 到期清理。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _mod():
    spec = importlib.util.spec_from_file_location(
        "ext_entry_scan_state", ROOT / "scripts" / "ext_entry_scan_state.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture()
def mod(tmp_path, monkeypatch):
    m = _mod()
    monkeypatch.setattr(m, "STATE_PATH", tmp_path / "scan_state.json")
    monkeypatch.delenv("EXT_SCAN_STATE_CACHE", raising=False)
    monkeypatch.delenv("EXT_SCAN_STATE_SHADOW", raising=False)
    m.reset_stats()
    m.reset_cache()
    return m


ROW = {"px": 42.0, "vol": 120_000, "chg": 3.5}


def test_switch_off_is_noop(mod, monkeypatch):
    """关闭 ⇒ lookup 恒 None、record 不写、不产生文件 (回滚语义: 行为字节级不变)。"""
    assert mod.record("AAPL", "premarket", "2026-09-23", "spread_wide", ROW) is False
    assert mod.lookup("AAPL", "premarket", "2026-09-23", ROW) is None
    assert not mod.STATE_PATH.exists()
    assert mod.stats()["hit"] == 0


def test_hit_and_saved_calls(mod, monkeypatch):
    monkeypatch.setenv("EXT_SCAN_STATE_CACHE", "true")
    assert mod.record("AAPL", "premarket", "2026-09-23", "spread_wide", ROW) is True
    hit = mod.lookup("AAPL", "premarket", "2026-09-23", dict(ROW))
    assert hit and hit["class"] == "quote_quality" and hit["shadow"] is False
    assert mod.stats()["hit"] == 1 and mod.stats()["saved"] == 2   # 价差+深度 = 2 次调用


def test_expired_is_miss(mod, monkeypatch):
    monkeypatch.setenv("EXT_SCAN_STATE_CACHE", "true")
    mod.record("AAPL", "premarket", "2026-09-23", "structure_vwap", ROW)
    st = mod._state("2026-09-23")
    for v in st["entries"].values():
        v["next_check_at"] = 1        # 已到期
    assert mod.lookup("AAPL", "premarket", "2026-09-23", ROW, state=st) is None
    assert mod.stats()["expired"] == 1 and mod.stats()["hit"] == 0


def test_jump_invalidates(mod, monkeypatch):
    """失效优先于命中: 价格跳 15% / 量跳 >50% / 涨跌幅跳 >1.5pp ⇒ 必须重评。"""
    monkeypatch.setenv("EXT_SCAN_STATE_CACHE", "true")
    mod.record("AAPL", "premarket", "2026-09-23", "liquidity", ROW)
    st = mod._state("2026-09-23")
    assert mod.lookup("AAPL", "premarket", "2026-09-23", {"px": 49.0, **{k: ROW[k] for k in ("vol", "chg")}}, state=st) is None
    assert mod.lookup("AAPL", "premarket", "2026-09-23", {"vol": 300_000, "px": 42.0, "chg": 3.5}, state=st) is None
    assert mod.lookup("AAPL", "premarket", "2026-09-23", {"chg": 6.0, "px": 42.0, "vol": 120_000}, state=st) is None
    assert mod.stats()["invalidated"] == 3
    assert mod.lookup("AAPL", "premarket", "2026-09-23", dict(ROW), state=st) is not None


def test_uncacheable_reason_not_stored(mod, monkeypatch):
    """类别表外 (廉价门/占位) ⇒ 不缓存, 避免用缓存替掉免费重算。"""
    monkeypatch.setenv("EXT_SCAN_STATE_CACHE", "true")
    for r in ("price_gate", "momentum_volume", "attempt_reserved", ""):
        assert mod.record("AAPL", "premarket", "2026-09-23", r, ROW) is False
    assert mod._state("2026-09-23")["entries"] == {}


def test_fail_close_short_ttl(mod, monkeypatch):
    """fail_close 必须短 TTL (10 分钟) —— 防把"数据迟到"钉成永久拒绝。"""
    monkeypatch.setenv("EXT_SCAN_STATE_CACHE", "true")
    mod.record("AAPL", "premarket", "2026-09-23", "actual_missing", ROW)
    mod.flush("2026-09-23")
    ent = next(iter(mod.load("2026-09-23")["entries"].values()))
    assert ent["class"] == "fail_close"
    assert 0 < ent["next_check_at"] - ent["cached_at"] <= 600


def test_cross_day_purged(mod, monkeypatch):
    monkeypatch.setenv("EXT_SCAN_STATE_CACHE", "true")
    mod.record("AAPL", "premarket", "2026-09-23", "liquidity", ROW)
    assert mod.load("2026-09-24")["entries"] == {}
    assert mod.lookup("AAPL", "premarket", "2026-09-24", ROW) is None


def test_corrupt_state_fail_open(mod, monkeypatch, tmp_path):
    """损坏/非法结构 ⇒ 空态 (宁多算一轮, 绝不少算一轮)。"""
    monkeypatch.setenv("EXT_SCAN_STATE_CACHE", "true")
    mod.STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    mod.STATE_PATH.write_text("{ not json", encoding="utf-8")
    assert mod.load("2026-09-23") == {"et_date": "2026-09-23", "entries": {}}
    mod.STATE_PATH.write_text(json.dumps(["wrong", "shape"]), encoding="utf-8")
    assert mod.lookup("AAPL", "premarket", "2026-09-23", ROW) is None


def test_shadow_mode_counts_but_does_not_enable(mod, monkeypatch):
    """影子: 记账命中, 但命中项标 shadow=True ⇒ 调用方不得用它替掉判定。"""
    monkeypatch.setenv("EXT_SCAN_STATE_SHADOW", "true")
    mod.record("AAPL", "premarket", "2026-09-23", "liquidity", ROW)
    hit = mod.lookup("AAPL", "premarket", "2026-09-23", dict(ROW))
    assert hit and hit["shadow"] is True
    assert mod.stats()["shadow_hit"] == 1 and mod.stats()["hit"] == 0


def test_write_failure_is_swallowed(mod, monkeypatch, tmp_path):
    monkeypatch.setenv("EXT_SCAN_STATE_CACHE", "true")
    monkeypatch.setattr(mod, "STATE_PATH", tmp_path / "nodir" / "x" / "s.json")
    monkeypatch.setattr(mod, "save", lambda st: False)
    assert mod.save({}) is False          # 不抛


def test_purge_and_observe_line(mod, monkeypatch):
    monkeypatch.setenv("EXT_SCAN_STATE_CACHE", "true")
    mod.record("AAPL", "premarket", "2026-09-23", "liquidity", ROW)
    st = mod._state("2026-09-23")
    assert mod.purge_expired(st) == 0     # 未到期不清
    for v in st["entries"].values():
        v["next_check_at"] = 1
    assert mod.purge_expired(st) == 1
    line = mod.observe_line("premarket", st)
    assert "扫描状态[premarket]" in line and "mode=on" in line

def test_gate_key_preserved(mod, monkeypatch):
    """命中的拒绝必须回到**同一个闸计数键** (闸计数主行契约: 逐键相等)。"""
    monkeypatch.setenv("EXT_SCAN_STATE_CACHE", "true")
    mod.record("AAPL", "premarket", "2026-09-23", "spread_wide", ROW, gate_key="liquidity")
    hit = mod.lookup("AAPL", "premarket", "2026-09-23", dict(ROW))
    assert hit["gate_key"] == "liquidity" and hit["reason"] == "spread_wide"


def test_executor_wiring_contract():
    """接线契约钉: 命中点必须在第一个逐票昂贵调用之前, 且四个拒绝点都记录结论 (防重构丢失)。"""
    src = (ROOT / "scripts" / "extended_hours_entry_executor.py").read_text(encoding="utf-8")
    i_hit = src.find("_scan_hit = _scan_state.lookup(")
    i_quote = src.find("quote = _liquidity_quote(qctx, symbol)", i_hit)
    assert i_hit > 0 and i_quote > i_hit, "命中点必须在逐票快照之前"
    assert src.count("_scan_state.record(symbol, session, today,") == 4, "四个昂贵拒绝点都要记录"
    assert "_scan_state.flush(today)" in src and "_scan_state.reset_stats()" in src
    assert "if _scan_hit and not _scan_hit.get(\"shadow\")" in src, "影子命中不得替掉判定"
