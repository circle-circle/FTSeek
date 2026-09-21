# -*- coding: utf-8 -*-
"""RFC-079 EOD 深水闸影子打点单测 (record-only 打点器).

钉锁:
  1. 开关关闭 → 零写入 (默认态 = 现网回滚态)
  2. 开关开启 → arm 行落盘, 字段完整, would_revoke 按 gate_pct 正确判定
  3. 同日同标的去重 (poll + sweeper 双面只落一行)
  4. pytest 环境隔离生效 (PYTEST_CURRENT_TEST 存在 → 零写入, 防污染生产账本)
  5. 豁免主链不受打点异常影响 (emit 内部异常全收口)
影子文件路径 monkeypatch 到 tmp_path, 不触生产 data/shadow/。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from app.playbook import overnight_decision as od  # noqa: E402


@pytest.fixture()
def shadow_tmp(tmp_path, monkeypatch):
    p = tmp_path / "eod_depth_gate_shadow.jsonl"
    monkeypatch.setattr(od, "_EDG_SHADOW_PATH", p)
    # pytest 在 call 阶段重设 PYTEST_CURRENT_TEST (fixture delenv 拦不住) →
    # 直接 patch 哨兵函数; 生产判定不读测试注入 (哨兵本体测试见下)
    monkeypatch.setattr(od, "_edg_pytest_guard", lambda: False)
    return p


def _cfg(enabled=True, gate=2.5):
    return {
        "eod_depth_gate_shadow_enabled": enabled,
        "eod_depth_gate_pct": gate,
        "loser_overnight_enabled": True,
        "loser_overnight_max_depth_pct": 99,
        "loser_overnight_max_hold_days": 5,
    }


def _emit(**kw):
    base = dict(decision_point="sweeper", symbol="US.TESTX", direction="LONG",
                source_type="PLAYBOOK_SIM", entry_price=100.0, qty=10,
                entry_time_str="2026-09-08T10:00:00-04:00", decision_price=97.0,
                profit_pct=-3.0, track="w_week")
    base.update(kw)
    od.emit_eod_depth_gate_shadow(**base)


def test_disabled_writes_nothing(shadow_tmp, monkeypatch):
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg(enabled=False))
    _emit()
    assert not shadow_tmp.exists()


def test_enabled_writes_would_revoke_row(shadow_tmp, monkeypatch):
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    _emit(profit_pct=-3.0)  # 水深 -3.0 ≤ -2.5 → would_revoke
    rows = [json.loads(l) for l in shadow_tmp.read_text().splitlines()]
    assert len(rows) == 1
    r = rows[0]
    assert r["shadow"] == "eod_depth_gate"
    assert r["would_revoke"] is True
    assert r["depth_net_pct"] == -3.0
    assert r["gate_pct"] == 2.5
    assert r["symbol"] == "US.TESTX"
    assert r["entry_date_et"] == "2026-09-08"
    assert r["track"] == "w_week"


def test_shallow_depth_not_revoke(shadow_tmp, monkeypatch):
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    _emit(profit_pct=-1.2, decision_point="poll_pre_close", track=None)
    rows = [json.loads(l) for l in shadow_tmp.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["would_revoke"] is False
    assert rows[0]["decision_point"] == "poll_pre_close"


def test_same_day_dedupe(shadow_tmp, monkeypatch):
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    _emit(decision_point="poll_pre_close")
    _emit(decision_point="sweeper")  # 同日同标的第二面 → 去重
    rows = [json.loads(l) for l in shadow_tmp.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["decision_point"] == "poll_pre_close"  # 首行保留


def test_pytest_isolation_blocks_production_write(tmp_path, monkeypatch):
    # 不删 PYTEST_CURRENT_TEST (pytest 运行时天然存在) + 生产路径指向 tmp —
    # 验证哨兵生效: 即使开关开也零写入
    p = tmp_path / "should_not_exist.jsonl"
    monkeypatch.setattr(od, "_EDG_SHADOW_PATH", p)
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "unit-test")
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    _emit()
    assert not p.exists()


def test_emit_swallows_internal_errors(shadow_tmp, monkeypatch):
    # 配置读取爆炸 → emit 不得抛出 (豁免主链保护)
    def _boom():
        raise RuntimeError("config down")
    monkeypatch.setattr("app.playbook.config.get_playbook_config", _boom)
    _emit()  # 不抛 = 通过
    assert not shadow_tmp.exists()


# ── RFC-095 (2026-09-22) 身份锚字段契约 ────────────────────────────────────────
# 消费方 = scripts/settle_eod_depth_gate_shadow.py::match_trade (结算侧按行认仓)。
# 单位/量纲声明 (军规: 新增落库字段必带单位声明 + 契约断言):
#   trade_ref  : 整数 = trade_records.id (最强锚); 缺失时 None (旧调用方未传)
#   entry_time : 字符串 = 该仓 entry_time **原样透传** (ISO 时间串, 三格式混存,
#                结算侧只做逐字比对, 不解析时区)
def test_identity_anchor_fields_present(shadow_tmp, monkeypatch):
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    _emit(trade_id=1503, entry_time_str="2026-09-15T10:48:09.508934-04:00")
    r = json.loads(shadow_tmp.read_text().splitlines()[0])
    assert r["trade_ref"] == 1503 and isinstance(r["trade_ref"], int)
    assert r["entry_time"] == "2026-09-15T10:48:09.508934-04:00"


def test_identity_anchor_absent_is_none(shadow_tmp, monkeypatch):
    # 旧调用方 (未传 trade_id) → trade_ref=None, 结算侧退化到 entry_time/价量锚
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    _emit(entry_time_str="")
    r = json.loads(shadow_tmp.read_text().splitlines()[0])
    assert r["trade_ref"] is None
    assert r["entry_time"] == ""


def test_identity_anchor_bad_type_falls_back_to_none(shadow_tmp, monkeypatch):
    # trade_id 传不可转 int 的值 → trade_ref 退化为 None, **行照常落盘** (度量连续性优先)
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    _emit(trade_id="not-a-number")
    r = json.loads(shadow_tmp.read_text().splitlines()[0])
    assert r["trade_ref"] is None
