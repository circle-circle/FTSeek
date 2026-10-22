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
from datetime import datetime, timedelta, timezone

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


def test_earnings_night_info_amc_bmo_and_missing_guard():
    guard = type("Guard", (), {})()
    guard._earnings_symbols = {
        "US.MU": {"report_date": "2026-09-30", "time_of_day": "amc"},
        "US.TEST": {"report_date": "2026-10-01", "time_of_day": "bmo"},
    }
    assert od._earnings_night_info("US.MU", guard, "2026-09-30") == (True, "2026-09-30", "AMC")
    assert od._earnings_night_info("US.TEST", guard, "2026-09-30") == (True, "2026-10-01", "BMO")
    assert od._earnings_night_info("US.MU", None, "2026-09-30") == (None, None, None)


# ── §14.1 结构性死字段回归 (DEB-20260929 round2) ──────────────────────────
#   上方测试用合成 'amc'/'bmo' 字面量, 恰好绕过生产真实值域 → 72 passed 是假绿:
#   富途源给的 time_of_day 是 'pre-market'/'post-market'/''，旧实现 .upper() 后与
#   'BMO'/'AMC' 比较永不命中 → is_earnings_night 恒 False (实测 19/19 只全 False)。
#   本组用**生产真实值域**钉死, 并钉住与单一真源 _parse_earnings_phase 同口径。
PROD_PHASE_VALUES = ("pre-market", "post-market", "", "N/A")


def test_production_phase_values_are_recognized_not_all_false():
    """生产真实值域下 is_earnings_night 必须能出 True — 防死字段复发。"""
    guard = type("Guard", (), {})()
    guard._earnings_symbols = {
        # AMC(盘后发) + arm 当日 == report_date → 财报夜成立
        "US.AIR": {"report_date": "2026-09-30", "time_of_day": "post-market"},
        # BMO(盘前发) + report_date == 次交易日 → 财报夜成立
        "US.KMX": {"report_date": "2026-10-01", "time_of_day": "pre-market"},
    }
    assert od._earnings_night_info("US.AIR", guard, "2026-09-30") == (
        True, "2026-09-30", "AMC")
    assert od._earnings_night_info("US.KMX", guard, "2026-09-30") == (
        True, "2026-10-01", "BMO")


def test_unknown_phase_fails_closed():
    """空值/不可识别相位 → UNKNOWN, fail-closed 不当财报夜 (含 CCL 型空值票)。"""
    guard = type("Guard", (), {})()
    guard._earnings_symbols = {
        "US.CCL": {"report_date": "2026-09-30", "time_of_day": ""},
        "US.NA": {"report_date": "2026-09-30", "time_of_day": "N/A"},
    }
    for sym in ("US.CCL", "US.NA"):
        is_night, rd, tod = od._earnings_night_info(sym, guard, "2026-09-30")
        assert is_night is False and tod == "UNKNOWN" and rd == "2026-09-30"


def test_normalize_matches_executor_single_source():
    """钉死与单一真源同口径 — 防两处归一逻辑漂移 (契约: 同一映射)。"""
    import scripts.extended_hours_entry_executor as EXEC
    for v in PROD_PHASE_VALUES + ("BMO", "AMC", "before-market",
                                  "after-hours", "PRE-MARKET", "post_market"):
        got = od._normalize_earnings_phase(v)
        before, after = EXEC._parse_earnings_phase(
            {"timeOfTheDay": v, "pub_type": ""})
        want = ("BMO" if before and not after else
                "AMC" if after and not before else "UNKNOWN")
        assert got == want, f"{v!r}: 本模块={got} 真源={want}"


def test_earnings_night_field_live_on_production_domain(shadow_tmp, monkeypatch):
    """端到端: 生产真实值域经 emit 落盘后 is_earnings_night 必须为 True。"""
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    fixed_now = datetime(2026, 9, 30, 15, 52, tzinfo=timezone(timedelta(hours=-4)))
    monkeypatch.setattr("app.utils.timezone.now_et", lambda: fixed_now)
    guard = type("Guard", (), {})()
    guard._earnings_symbols = {
        "US.TESTX": {"report_date": "2026-09-30", "time_of_day": "post-market"}}
    _emit(profit_pct=-1.0, earnings_guard=guard)
    row = json.loads(shadow_tmp.read_text().splitlines()[0])
    assert row["is_earnings_night"] is True
    assert row["earnings_time_of_day"] == "AMC"



def test_earnings_night_fields_are_record_only(shadow_tmp, monkeypatch):
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    fixed_now = datetime(2026, 9, 30, 15, 52, tzinfo=timezone(timedelta(hours=-4)))
    monkeypatch.setattr("app.utils.timezone.now_et", lambda: fixed_now)
    guard = type("Guard", (), {})()
    guard._earnings_symbols = {
        "US.TESTX": {"report_date": "2026-09-30", "time_of_day": "amc"}}
    _emit(profit_pct=-1.0, earnings_guard=guard)
    row = json.loads(shadow_tmp.read_text().splitlines()[0])
    assert row["is_earnings_night"] is True
    assert row["earnings_report_date"] == "2026-09-30"
    assert row["earnings_time_of_day"] == "AMC"
    assert row["earnings_night_would_revoke"] is True
    assert row["would_revoke"] is False


# ── RFC-160 (2026-10-22): arm 行 mfe_pct_at_arm 字段钉 ─────────────────────
def _read_rows(p):
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def test_mfe_field_long(shadow_tmp, monkeypatch):
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    _emit(peak_price=101.5)  # LONG entry=100 → MFE +1.5%
    rows = _read_rows(shadow_tmp)
    assert rows[0]["mfe_pct_at_arm"] == 1.5


def test_mfe_field_short(shadow_tmp, monkeypatch):
    """SHORT 方向感知: peak=最有利价 (低价), MFE = (entry−peak)/entry。"""
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    _emit(direction="SHORT", peak_price=98.0)
    rows = _read_rows(shadow_tmp)
    assert rows[0]["mfe_pct_at_arm"] == 2.0


def test_mfe_field_missing_peak_is_null(shadow_tmp, monkeypatch):
    """无 peak_price (旧调用方/重启前仓位) → null 落行, 不进 MFE 轨分母 (不猜)。"""
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    _emit()
    rows = _read_rows(shadow_tmp)
    assert rows[0]["mfe_pct_at_arm"] is None


def test_mfe_field_does_not_change_depth_track(shadow_tmp, monkeypatch):
    """MFE 字段是纯增量: would_revoke/depth 等 B19 既有字段语义零变化。"""
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: _cfg())
    _emit(peak_price=100.4)
    rows = _read_rows(shadow_tmp)
    assert rows[0]["would_revoke"] is True       # profit_pct=-3.0 ≤ -2.5
    assert rows[0]["depth_net_pct"] == -3.0
    assert rows[0]["mfe_pct_at_arm"] == 0.4
