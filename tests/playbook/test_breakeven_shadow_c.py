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

    def test_c_fires_when_held_ge_60(self, iso):
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

    def test_c_not_fires_when_held_lt_60(self, iso):
        """held=30min < 60 门 → 只写 B, 不写 C (动量可能未走完)."""
        reason, _, _ = _run(current_price=100.5, peak_price=101.0,
                            held_minutes=30)
        assert reason == ""
        variants = [r["variant"] for r in _read_shadow(iso)]
        assert "B_buffer_exit" in variants
        assert "C_late_buffer_touch" not in variants, "held<60 不应写C"

    def test_c_fires_after_b_already_fired(self, iso):
        """B 已写过 (_be_shadow_b 预置) → 后续 held≥60 触碰只补 C, B 不重复."""
        reason, _, _ = _run(current_price=100.5, peak_price=101.0,
                            held_minutes=130, shadow_b_prefired=True)
        assert reason == ""
        recs = _read_shadow(iso)
        variants = [r["variant"] for r in recs]
        assert variants.count("B_buffer_exit") == 0, "B已写过不应重复"
        assert variants.count("C_late_buffer_touch") == 1

    def test_test_symbol_writes_nothing(self, iso):
        """_symbol=TEST → 测试卫生守卫生效, 不写任何记录."""
        reason, _, _ = _run(current_price=100.5, peak_price=101.0,
                            held_minutes=130, symbol="TEST")
        assert reason == ""
        assert _read_shadow(iso) == [], "TEST 符号不应写影子 jsonl"

    def test_short_symmetry(self, iso):
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
