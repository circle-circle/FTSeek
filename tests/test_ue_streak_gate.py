"""U7 续期硬闸测试 (2026-08-25, REVIEW-20260824 裁决GO 落地, 提案 §八.6 清单4)。

判据: streak=None/1/2/3 四态过滤行为 + 配置缺键默认全放行。
"""
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.llm_universe_scan import _streak_passes  # noqa: E402


def _s(streak):
    return {"symbol": "TEST", "emergence_streak_days": streak}


class TestStreakPasses:
    """谓词四态 (max_streak=2 生效口径)."""

    def test_streak_none_passes_as_first_day(self):
        # 埋点失败/缺失 → 视作首日=1, 放行 (不误杀)
        assert _streak_passes(_s(None), 2) is True

    def test_streak_zero_passes_as_first_day(self):
        assert _streak_passes(_s(0), 2) is True

    def test_streak_1_passes(self):
        assert _streak_passes(_s(1), 2) is True

    def test_streak_2_passes_boundary(self):
        # <= max 放行, 边界 2=2 通过
        assert _streak_passes(_s(2), 2) is True

    def test_streak_3_blocked(self):
        assert _streak_passes(_s(3), 2) is False

    def test_streak_8_blocked(self):
        assert _streak_passes(_s(8), 2) is False

    def test_missing_key_passes(self):
        assert _streak_passes({"symbol": "TEST"}, 2) is True


class TestDefaultAllPass:
    """配置缺键 → 默认99 = 全放行 (零行为回滚语义)."""

    def test_streak_99_default_allows_anything(self):
        for v in (None, 0, 1, 2, 3, 8, 50, 99):
            assert _streak_passes(_s(v), 99) is True, f"streak={v} 应在缺键默认下放行"

    def test_cfg_missing_key_returns_99_default(self):
        # _cfg 读不到键 → 默认 99 → 闸不生效 (提案 §八.6: 防配置未读到静默启闸)
        from scripts import llm_universe_scan as ue
        with mock.patch.object(ue, "_cfg", return_value=None):
            _max = int(ue._cfg("universe_expansion.max_emergence_streak", 99) or 99)
        assert _max == 99

    def test_streak_gate_off_when_default(self):
        # 缺键场景下, 连续续期 30 天的标的也不拦
        assert _streak_passes(_s(30), 99) is True
