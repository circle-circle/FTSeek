# -*- coding: utf-8 -*-
"""P1 深跳空逆势 BUY 降仓契约测试 (2026-08-20, DEB §44/§47, 用户裁决).

证据: gap≤-3% 逆势 BUY 全历史 n=14 净 -772 胜率 21%; 顺势 SHORT 同条件 n=5
净 +11.84 → 只拦 BUY. gap-through 跳穿止损, 仅仓位端可缓解.

契约保证:
  A. gap≤阈值 的 BUY 触发降仓判定 (含边界 ==阈值)
  B. SHORT / 浅跳空 / 缺价 / size≤1 → 不降仓 (fail-open)
  C. 方向不对称: 同 gap 的 SHORT 永不触发
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from app.playbook.playbook_service import _gap_dive_should_halve  # noqa: E402


class TestGapDiveHalve:
    def test_deep_gap_buy_triggers(self):
        assert _gap_dive_should_halve("BUY", 100.0, 95.0, 6) is True  # -5%

    def test_boundary_gap_triggers(self):
        assert _gap_dive_should_halve("BUY", 100.0, 97.0, 6) is True  # 恰 -3%

    def test_shallow_gap_no_trigger(self):
        assert _gap_dive_should_halve("BUY", 100.0, 98.0, 6) is False  # -2%

    def test_short_never_triggers(self):
        """方向不对称: 顺势 SHORT 同条件净 +11.84, 永不降仓."""
        assert _gap_dive_should_halve("SELL_SHORT", 100.0, 95.0, 6) is False

    def test_size_one_no_trigger(self):
        """size=1 已不可再减半 → 不触发 (max(1,...) 无意义的调用)."""
        assert _gap_dive_should_halve("BUY", 100.0, 95.0, 1) is False

    def test_missing_price_fail_open(self):
        assert _gap_dive_should_halve("BUY", 0, 95.0, 6) is False
        assert _gap_dive_should_halve("BUY", 100.0, 0, 6) is False
        assert _gap_dive_should_halve("BUY", None, None, 6) is False

    def test_custom_threshold(self):
        assert _gap_dive_should_halve("BUY", 100.0, 96.0, 6, halve_pct=-2.0) is True
        assert _gap_dive_should_halve("BUY", 100.0, 96.0, 6, halve_pct=-5.0) is False

    def test_up_gap_no_trigger(self):
        assert _gap_dive_should_halve("BUY", 100.0, 103.0, 6) is False  # 高开
