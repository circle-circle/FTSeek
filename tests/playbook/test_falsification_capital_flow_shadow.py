"""[2026-10-09 复查修复] 证伪门·资金流背离影子 (record-only) 落盘契约。

背景: pipeline 拆分 (DEB-20261008 路线图阶段10) 把 ``_check_intraday_trigger`` 本体
迁入 ``pipeline_parts/trigger_flow.py`` 时, ②号资金流背离影子写盘块漏带
``Path``/``json``/``datetime`` 三名 (HEAD 里它们是 pipeline 模块级 import),
模块命名空间实证三名皆无 ⇒ 必 NameError ⇒ 被块内 ``except Exception`` 吞成一条
warning ⇒ **影子静默死亡**。该影子是「十月重裁」判据的数据底座 (用户 2026-08-23
裁决 record-only 攒样), 丢行 = 攒样进度虚高。零测试覆盖是它穿过 1118 绿的原因。

本文件钉住 (修复后必须全绿):
  1. 背离触发时影子行**真实落盘** (jsonl 一行, 关键字段齐全);
  2. 全程无「落盘失败」warning (有 warning = except 又吃到异常 = 回归);
  3. 影子保持 record-only: 不翻转 triggered, 不拦单 (用户裁决语义);
  4. ``pipeline._capital_flow_divergence`` monkeypatch 缝经转发器注入仍然有效
     (本测试若缝断则影子恒不触发 ⇒ 用例 1 必红, 天然双向钉)。

harness 克隆自 test_low_atr_gate_screening_basis.py (同款密封档), 只改三门开关。
"""
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.playbook import pipeline as pipe  # noqa: E402

# 与 test_low_atr_gate_screening_basis.py 同款密封档, 差异: 低ATR门关、证伪门开。
CFG = {
    "entry_time_cutoff_enabled": False,
    "entry_open_blackout_enabled": False,
    "earnings_blackout_gate_enabled": False,
    "low_atr_gate_enabled": False,
    "symbol_streak_ban_enabled": False,
    "intraday_vol_gate_enabled": False,
    "entry_vwap_gate_enabled": False,
    "entry_reanchor_enabled": True,
    "entry_reanchor_max_drift_pct": 4.0,
    "thesis_drift_invalidate_enabled": True,
    "falsification_gates_enabled": True,
    "falsification_gate_vwap_slope_enabled": True,   # 保 _fals_slope 有定义 (MagicMock 路由 → None)
    "falsification_gate_capital_flow_shadow": True,
}


@pytest.fixture(autouse=True)
def _sealed_pb_cfg(monkeypatch):
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: dict(CFG))


def _scheduler(price=99.0):
    """BUY entry=100 / price=99 ⇒ 触发带内 (与低ATR门文件同一几何)。"""
    sch = MagicMock()
    sch.data_router.get_full_market_context.return_value = {
        "price": price, "day_high": 101.0, "day_low": 98.0,
        "volume_5d_avg_ratio": 2.0,
    }
    return sch


def _inst(**over):
    # 注意: 影子块自带 "TEST" not in symbol 守卫, 故用 US.ACME 而非惯用的 US.TESTA。
    base = {"symbol": "US.ACME", "direction": "BUY", "entry_price": 100.0,
            "id": 424242}
    base.update(over)
    return base


@pytest.fixture
def sandboxed_module_file(monkeypatch, tmp_path):
    """把 pipeline.__file__ 指进 tmp 沙箱: 转发器在调用时读模块全局 __file__
    注入 module_file, 影子路径 parents[2]/data ⇒ 落进 tmp_path/data/,
    不碰生产 data/falsification_gate_shadow.jsonl。"""
    fake = tmp_path / "app" / "playbook" / "pipeline.py"
    fake.parent.mkdir(parents=True)
    fake.write_text("# sandbox\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(pipe, "__file__", str(fake))
    return tmp_path / "data" / "falsification_gate_shadow.jsonl"


class TestCapitalFlowShadowWrites:
    def test_divergence_row_actually_persisted(self, sandboxed_module_file, caplog,
                                               monkeypatch):
        monkeypatch.setattr(pipe, "_capital_flow_divergence",
                            lambda symbol, is_short: True)
        with caplog.at_level(logging.WARNING):
            triggered = pipe._check_intraday_trigger(_scheduler(), _inst())

        assert triggered is True, "record-only 影子不得拦单 (用户 2026-08-23 裁决)"
        assert sandboxed_module_file.exists(), \
            "影子行未落盘 — 2026-10-09 的 NameError 哑弹回归 (except 吞错后仅剩 warning)"
        rows = [json.loads(l) for l in
                sandboxed_module_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(rows) == 1
        row = rows[0]
        assert row["gate"] == "capital_flow"
        assert row["symbol"] == "US.ACME"
        assert row["direction"] == "BUY"
        assert row["diverged"] is True and row["would_block"] is True
        assert row["triggered"] is True
        assert row["inst_id"] == 424242
        assert "ts" in row and "date" in row and row["tz"] == "ET"
        assert not [r for r in caplog.records if "落盘失败" in r.getMessage()], \
            "出现「落盘失败」warning = except 又吃到异常 (影子丢失)"

    def test_no_divergence_no_row(self, sandboxed_module_file, monkeypatch):
        """对照组: 无背离 (None) 时不落行 — 防「影子变恒写」的反向腐化。"""
        monkeypatch.setattr(pipe, "_capital_flow_divergence",
                            lambda symbol, is_short: None)
        triggered = pipe._check_intraday_trigger(_scheduler(), _inst())
        assert triggered is True
        assert not sandboxed_module_file.exists()
