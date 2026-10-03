# -*- coding: utf-8 -*-
"""W_WEEK 水深上限契约测试 (2026-08-20, DEB §47 方案a, 用户裁决).

8/16 后 W_WEEK 实弹净 -810.55 (修复率 1/5=20% < 平衡线 55.5%):
决策时刻水深 >2% 的仓 (MU/FIX -3.1%) 0 修复 2 灾难, 属"入场即错"非修复窗口.
水深上限加在 _loser_overnight_exempt — EOD/DEAD_SIGNAL/TIME_EXIT/MAX_HOLD
四处豁免唯一入口 → 单点生效全链. SL 仍最高优先级硬兜底不受影响.

契约保证:
  A. 水深 ≤上限 → 豁免维持 (浅亏修复窗口保留, UBER -1.51% 类)
  B. 水深 >上限 → 撤销豁免 (转 EOD/DEAD_SIGNAL/TIME_EXIT 链)
  C. 上限=99 → 等同禁用 (回滚行为, 任何深度都豁免)
  D. 非水下/非 SIM/开关关 → 原行为不变
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

from app.playbook.pipeline import _loser_overnight_exempt  # noqa: E402
from app.utils.timezone import now_et  # noqa: E402


class _FakeCfgDepth:
    """Fake config: 覆盖 loser_overnight_max_depth_pct, 其余走真实配置."""

    def __init__(self, max_depth):
        from app.playbook.config import get_playbook_config as _real_get
        self._real = _real_get()
        self._max_depth = max_depth

    def get(self, key, default=None):
        if key == "loser_overnight_max_depth_pct":
            return self._max_depth
        if key == "loser_overnight_enabled":
            return True
        return self._real.get(key, default)


class TestWWeekDepthCap:
    @pytest.fixture()
    def cfg(self, monkeypatch):
        fake = _FakeCfgDepth(2.0)
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: fake)
        return fake

    def test_shallow_underwater_still_exempt(self, cfg):
        """UBER 型 (-1.51%): 浅亏仍在修复窗口 → 豁免."""
        assert _loser_overnight_exempt("PLAYBOOK_SIM", now_et().isoformat(), -1.5) is True

    def test_deep_underwater_exemption_revoked(self, cfg):
        """MU/FIX 型 (-3.1%): 入场即错 → 撤销豁免."""
        assert _loser_overnight_exempt("PLAYBOOK_SIM", now_et().isoformat(), -2.5) is False
        assert _loser_overnight_exempt("PLAYBOOK_SIM", now_et().isoformat(), -3.1) is False

    def test_boundary_at_cap_still_exempt(self, cfg):
        """恰 -2.0% 在上限边缘 → 仍豁免 (条件是 < -max_depth)."""
        assert _loser_overnight_exempt("PLAYBOOK_SIM", now_et().isoformat(), -2.0) is True

    def test_cap_99_disables(self, cfg):
        """上限=99 → 回滚行为: 任何深度都豁免."""
        cfg._max_depth = 99
        assert _loser_overnight_exempt("PLAYBOOK_SIM", now_et().isoformat(), -50.0) is True

    def test_profit_not_exempt(self, cfg):
        """盈利仓不豁免 (原行为)."""
        assert _loser_overnight_exempt("PLAYBOOK_SIM", now_et().isoformat(), 0.5) is False

    def test_live_same_as_sim(self, cfg):
        """RFC-130 (2026-10-03 用户裁「sim和实盘策略一致」): LIVE 与 SIM 同语义.

        原 test_live_not_exempt 断言 LIVE 恒不豁免 — 该 SIM-only 分叉已删除,
        LIVE 臂继承同一 W_WEEK 豁免 (水下 + 未达持有上限即豁免)。
        """
        assert _loser_overnight_exempt("PLAYBOOK_LIVE", now_et().isoformat(), -1.0) is True
        assert _loser_overnight_exempt("PLAYBOOK_LIVE", now_et().isoformat(), -1.0) == \
               _loser_overnight_exempt("PLAYBOOK_SIM", now_et().isoformat(), -1.0)

    def test_shadow_source_still_not_exempt(self, cfg):
        """非双臂白名单源 (SHADOW/其他) 仍不豁免 — 对齐只覆盖 SIM/LIVE 双臂。"""
        assert _loser_overnight_exempt("PLAYBOOK_SHADOW", now_et().isoformat(), -1.0) is False
        assert _loser_overnight_exempt("MAIN_PIPELINE", now_et().isoformat(), -1.0) is False
