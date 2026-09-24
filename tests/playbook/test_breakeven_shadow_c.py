# -*- coding: utf-8 -*-
"""影子C (C_late_buffer_touch) 时间门控记录契约测试 (2026-08-19, DEB-20260819 §三十七).

背景: B 结算(n=49)事后发现 — 锚触碰≥60min 组 14例13胜 +$76 (死单特征),
<60min 组被 7 笔错过赢单拖成 -$88 (动量未走完)。C = 只对 held≥60min 的首次
锚触碰记录反事实出场 (record-only, 不改真实仓位行为)。

契约:
  1. held≥60min 锚触碰 → 写 C 记录 (variant=C_late_buffer_touch, gate_min=60)
  2. held<60min 锚触碰 → 只写 B, 不写 C (时间门关闭)
  3. B 已写过 (meta._be_shadow_b 预置) → 只补 C, 不重复 B (每仓每变体至多一条)
  4. _symbol=="TEST" → 不写任何记录 (测试卫生守卫: 8/18-19 生产 jsonl 已被
     test_net_breakeven_contract 写入 TEST 记录, 实证污染)
  5. SHORT 方向对称

隔离: monkeypatch.chdir(tmp_path) — 影子写盘用相对路径 data/breakeven_shadow.jsonl;
config 经 __file__ 绝对路径加载且为单例 (app/playbook/config.py L87/L155),
chdir 不影响。生产 jsonl 不被测试触碰。
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from app.playbook.pipeline import _check_playbook_exit_rules  # noqa: E402
from app.utils.timezone import now_et  # noqa: E402


class _FakeCfgSlipZero:
    """Fake config: 注入 slip=0 (其余键透传真 config).

    2026-08-20 方案A 后生产 slip=0.2, 真实保本触发线 (fee+0.2%) 高于 B 锚
    (fee+0.15%) → 影子 B/C 在生产被真实退出取代。本契约测试用 slip=0 保留
    原语义; 生产取代行为由 TestShadowCSupersededByPlanA 覆盖。
    注意: 实例必须在 monkeypatch 之前构造 (否则 __init__ 回调已 patch 的函数死循环).
    """

    def __init__(self):
        from app.playbook.config import get_playbook_config as _real_get
        self._real = _real_get()

    def get(self, key, default=None):
        if key == "breakeven_slip_cushion_pct":
            return 0.0
        # adaptive_trailing_enabled (生产 2026-09 开启, drawdown 0.8→0.4%)
        # 收紧 effective_sl 到 100.596 > current 100.5 → SL_TRAIL_LOCK 先于影子
        # 锚触发, 破坏本测试的价格带假设. 本契约测的是影子C记录逻辑, 不是
        # trailing stop 参数, 关掉 adaptive 保留原语义 (与 slip=0 同理).
        if key == "adaptive_trailing_enabled":
            return False
        return self._real.get(key, default)

    def get_horizon_top(self, key, default=None):
        return self._real.get_horizon_top(key, default)


@pytest.fixture()
def slip_zero(monkeypatch):
    fake = _FakeCfgSlipZero()
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: fake)
    return fake


def _run(current_price, peak_price, held_minutes, symbol="US_FAKE",
         direction="LONG", entry_price=100.0, initial_sl=95.0, qty=10,
         shadow_b_prefired=False):
    meta = {
        "_log_tag": "test_shadow_c", "_symbol": symbol, "cohort": "A",
        "horizon": "short", "_source_type": "PLAYBOOK_SIM",
        "_skip_allowed": False, "_qty": qty,
    }
    if shadow_b_prefired:
        meta["_be_shadow_b"] = True
    reason, exit_px, peak_out, be_trig = _check_playbook_exit_rules(
        direction=direction, entry_price=entry_price, current_price=current_price,
        initial_sl=initial_sl, peak_price=peak_price,
        meta=meta,
        held_minutes=held_minutes, minutes_to_close=30,
        minutes_from_open=130, entry_time_str=now_et().isoformat(),
    )
    return reason, be_trig, meta


def _read_shadow(tmp_path):
    fp = tmp_path / "data" / "breakeven_shadow.jsonl"
    if not fp.exists():
        return []
    return [json.loads(x) for x in fp.read_text().splitlines() if x.strip()]


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """chdir 隔离 + data/ 目录预建 (open 不会自建目录, 失败被 except 静默吞)."""
    (tmp_path / "data").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestShadowCContract:

    def test_c_fires_when_held_ge_60(self, iso, slip_zero):
        """held=130min ≥ 60 门 → B+C 各一条, C 带 gate_min=60.

        价格带: 净保本线 100.4071 < current 100.5 ≤ B锚 100.5575
        → 未触发 SL_BREAKEVEN (真实仓继续), 锚已碰 (反事实记录)."""
        reason, be_trig, _ = _run(current_price=100.5, peak_price=101.0,
                                   held_minutes=130)
        assert reason == "", f"价格带内不应有任何真实退出, got {reason}"
        assert be_trig, "peak=1.0% 应已武装 breakeven"
        recs = _read_shadow(iso)
        variants = [r["variant"] for r in recs]
        assert "B_buffer_exit" in variants, "首次锚触碰应写B"
        assert "C_late_buffer_touch" in variants, "held≥60 锚触碰应写C"
        c = [r for r in recs if r["variant"] == "C_late_buffer_touch"][0]
        assert c["gate_min"] == 60
        assert c["held_minutes"] == 130.0
        assert c["symbol"] == "US_FAKE"
        assert abs(c["b_anchor"] - round(100.4071 * 1.0015, 4)) < 0.01

    def test_c_not_fires_when_held_lt_60(self, iso, slip_zero):
        """held=30min < 60 门 → 只写 B, 不写 C (动量可能未走完)."""
        reason, _, _ = _run(current_price=100.5, peak_price=101.0,
                            held_minutes=30)
        assert reason == ""
        variants = [r["variant"] for r in _read_shadow(iso)]
        assert "B_buffer_exit" in variants
        assert "C_late_buffer_touch" not in variants, "held<60 不应写C"

    def test_c_fires_after_b_already_fired(self, iso, slip_zero):
        """B 已写过 (_be_shadow_b 预置) → 后续 held≥60 触碰只补 C, B 不重复."""
        reason, _, _ = _run(current_price=100.5, peak_price=101.0,
                            held_minutes=130, shadow_b_prefired=True)
        assert reason == ""
        recs = _read_shadow(iso)
        variants = [r["variant"] for r in recs]
        assert variants.count("B_buffer_exit") == 0, "B已写过不应重复"
        assert variants.count("C_late_buffer_touch") == 1

    def test_test_symbol_writes_nothing(self, iso, slip_zero):
        """_symbol=TEST → 测试卫生守卫生效, 不写任何记录."""
        reason, _, _ = _run(current_price=100.5, peak_price=101.0,
                            held_minutes=130, symbol="TEST")
        assert reason == ""
        assert _read_shadow(iso) == [], "TEST 符号不应写影子 jsonl"

    def test_short_symmetry(self, iso, slip_zero):
        """SHORT 对称: 净保本线 99.5929 > current 99.50 ≥ B锚 99.4435 → 写 C."""
        reason, _, _ = _run(current_price=99.5, peak_price=99.0,
                            held_minutes=130, direction="SHORT",
                            initial_sl=105.0)
        assert reason == "", f"SHORT价格带内不应有真实退出, got {reason}"
        variants = [r["variant"] for r in _read_shadow(iso)]
        assert "C_late_buffer_touch" in variants
        c = [r for r in _read_shadow(iso) if r["variant"] == "C_late_buffer_touch"][0]
        assert c["direction"] == "SHORT"
        assert c["shadow_exit_price"] == 99.5

class TestShadowCSupersededByPlanA:
    """方案A 生产语义 (slip=0.2): 真实保本触发线 (fee+0.2%≈100.607) 高于 B 锚
    (fee+0.15%≈100.5575) → current=100.5 先触发真实 SL_BREAKEVEN, 影子 B/C 不记录."""

    @pytest.fixture()
    def slip_prod(self, monkeypatch):
        from tests.playbook.test_net_breakeven_contract import _FakeCfgSlip
        fake = _FakeCfgSlip(slip=0.2)
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: fake)
        return fake

    def test_real_exit_supersedes_shadow(self, iso, slip_prod):
        """生产 slip=0.2: 100.5 < 触发线 100.607 → 真实 SL_BREAKEVEN, 影子零记录."""
        reason, be_trig, _ = _run(current_price=100.5, peak_price=101.0,
                                  held_minutes=130)
        assert reason == "SL_BREAKEVEN", \
            f"方案A下垫线内价格应真实保本出场, got {reason}"
        assert be_trig
        assert _read_shadow(iso) == [], "真实退出先于影子锚 → 影子零记录 (取代)"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
