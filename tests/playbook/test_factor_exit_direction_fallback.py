# -*- coding: utf-8 -*-
"""factor_exit 方向感知回退契约测试 (F1, 2026-09-04).

锁定两个已生效修复的回归行为 (_get_factor_exit_config, pipeline.py):
  ① 7/7 fix: 方向感知匹配 — 第一轮优先匹配与 direction 一致的 condition
     (SHORT 优先 BEARISH_CONDITIONS, LONG 优先 BULLISH_CONDITIONS)。
  ② P0-2 fix (2026-07-20): 第二轮回退跳过方向相反的 condition —
     SHORT 指令的 bullish conditions (如 macd_golden_cross) 不得配上
     bullish 因子退出规则 (修复前无方向过滤, 会错误命中并落 trailing/dd
     更激进的多头规则), 无命中则落 default。

背景: P0-2 修复于 2026-07-20 上线但无回归测试锁定 (tests 全目录
grep `_get_factor_exit_config` 零匹配), 本测试补齐 — 若有人回退
第二轮的方向过滤, 此处立即变红。

契约保证 (合成规则, 值有区分度, 不随 yaml 漂移):
  A. SHORT + 纯 bullish conditions → default (P0-2 核心)
  B. SHORT + [bullish, bearish] → bearish 规则 (第一轮方向感知)
  C. SHORT + [bullish, neutral] → neutral 规则 (第二轮跳过 bullish, 中性可回退)
  D. LONG + 纯 bearish conditions → default (对称性)
  E. direction=None → 保留原始逻辑 (第一个有 exit_rule 的 condition)
  F. 空 conditions → default
  G. 真实 playbook_horizon.yaml 冒烟: SHORT + macd_golden_cross 落 default 引用
"""
import os
import sys

import pytest
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from app.playbook.pipeline import _get_factor_exit_config  # noqa: E402
from app.playbook.condition_taxonomy import (  # noqa: E402
    BULLISH_CONDITIONS,
    BEARISH_CONDITIONS,
)

# ── 合成规则 (be/dd 值各不相同, 断言可区分命中来源) ──
_RULES = {
    "default": {"breakeven_at_pct": 0.40, "drawdown_from_peak_pct": 0.80},
    # bullish taxonomy 成员
    "macd_golden_cross": {"breakeven_at_pct": 0.51, "drawdown_from_peak_pct": 0.51},
    "ma_golden_cross": {"breakeven_at_pct": 0.52, "drawdown_from_peak_pct": 0.52},
    # bearish taxonomy 成员
    "macd_dead_cross": {"breakeven_at_pct": 0.21, "drawdown_from_peak_pct": None},
    # 中性 (不在 BULLISH/BEARISH 任一集合)
    "above_ma20": {"breakeven_at_pct": 0.33, "drawdown_from_peak_pct": 0.33},
}


@pytest.fixture(autouse=True)
def _assert_taxonomy_preconditions():
    """前置契约: 测试选用的 condition 分类必须与生产 taxonomy 一致。

    taxonomy 若未来重新分类 (如 above_ma20 归入 BULLISH), 本组测试语义失效 —
    与其静默测了个寂寞, 不如显式失败并提醒同步用例。
    """
    assert "macd_golden_cross" in BULLISH_CONDITIONS
    assert "ma_golden_cross" in BULLISH_CONDITIONS
    assert "macd_dead_cross" in BEARISH_CONDITIONS
    assert "above_ma20" not in BULLISH_CONDITIONS
    assert "above_ma20" not in BEARISH_CONDITIONS
    yield


class TestShortFallbackSkipsOpposite:
    """P0-2: SHORT 指令第二轮回退跳过方向相反 (bullish) 因子。"""

    def test_short_pure_bullish_conditions_fall_to_default(self):
        """A. SHORT + 纯 bullish conditions → default (修复前会错误命中 golden 规则)。"""
        got = _get_factor_exit_config(
            ["macd_golden_cross", "ma_golden_cross"], _RULES, direction="SHORT"
        )
        assert got is _RULES["default"]

    def test_short_second_round_matches_neutral_after_skipping_bullish(self):
        """C. SHORT + [bullish, neutral] → 中性规则可回退命中。"""
        got = _get_factor_exit_config(
            ["macd_golden_cross", "above_ma20"], _RULES, direction="SHORT"
        )
        assert got is _RULES["above_ma20"]

    def test_short_prefers_direction_matched_rule_first_round(self):
        """B. SHORT + [bullish, bearish] → 第一轮命中 bearish 规则。"""
        got = _get_factor_exit_config(
            ["macd_golden_cross", "macd_dead_cross"], _RULES, direction="SHORT"
        )
        assert got is _RULES["macd_dead_cross"]


class TestLongSymmetry:
    """P0-2 对称性: LONG 指令同样跳过方向相反 (bearish) 因子。"""

    def test_long_pure_bearish_conditions_fall_to_default(self):
        """D. LONG + 纯 bearish conditions → default。"""
        got = _get_factor_exit_config(["macd_dead_cross"], _RULES, direction="LONG")
        assert got is _RULES["default"]

    def test_long_prefers_direction_matched_rule_first_round(self):
        got = _get_factor_exit_config(
            ["macd_dead_cross", "macd_golden_cross"], _RULES, direction="LONG"
        )
        assert got is _RULES["macd_golden_cross"]


class TestBackwardCompat:
    """原始逻辑保留 (direction=None / 空 conditions)。"""

    def test_direction_none_returns_first_matching_rule(self):
        """E. direction=None → 第一个有 exit_rule 的 condition (无方向过滤)。"""
        got = _get_factor_exit_config(["macd_golden_cross"], _RULES, direction=None)
        assert got is _RULES["macd_golden_cross"]

    def test_empty_conditions_returns_default(self):
        """F. 空 conditions → default。"""
        assert _get_factor_exit_config([], _RULES, direction="SHORT") is _RULES["default"]
        assert _get_factor_exit_config([], _RULES) is _RULES["default"]


class TestRealYamlSmoke:
    """真实 playbook_horizon.yaml 冒烟 (引用相等断言, 不依赖规则值差异)。"""

    def test_short_bullish_condition_falls_to_default_in_real_yaml(self):
        """G. 真实 yaml: SHORT + macd_golden_cross → default 引用 (P0-2 生效于生产配置)。"""
        yaml_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "..",
            "config", "playbook_horizon.yaml",
        )
        with open(yaml_path, "r", encoding="utf-8") as f:
            factor_rules = yaml.safe_load(f).get("factor_exit_rules", {})
        assert "macd_golden_cross" in factor_rules, "yaml 缺 macd_golden_cross 规则 (用例过时)"
        got = _get_factor_exit_config(["macd_golden_cross"], factor_rules, direction="SHORT")
        assert got is factor_rules["default"]
