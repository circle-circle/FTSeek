"""[P1.2 2026-09-12, DEB-20260910 / proud-slope-falcon] 执行侧低ATR门读筛选快照同义字段.

被测: `app/playbook/pipeline_parts/trigger_flow.py::check_intraday_trigger` 的低ATR门 +
      `_screening_age_hours` + `_emit_trigger_miss(extra=...)`。

修前病灶: 门只读 `inst["atr_pct"]` (盘前定桶写入的**止损口径** get_atr_pct),
缺失即 `except/fail-open` ⇒ 对无桶行**门恒开**; 而 P1 新增的候选筛选快照
(`raw_instruction.candidate_screening.screening_atr_ratio`, 原始已完成日线
TR14 rolling-mean / 参考价) 明明带了同义字段却无人消费 —— `get_instruction_screening`
是 write-only。计划 P1.2 要求"筛选快照传到原执行低ATR门, 读取同义字段"。

钉住的不变量:
  1. 主口径不变: 有 `atr_pct` 时判定与修前逐字一致 (阈值/比较方向/拦截动作),
     不重定义 atr_pct/vol_bucket 的止损含义;
  2. 兜底只在行内值缺失/不可用时启用 (不抢主口径, 不放宽也不收紧主口径);
  3. 量纲声明: `atr_pct` 与 `screening_atr_ratio` **同为比例** (0.03 = 3%),
     实测 DB 728 行 atr_pct ∈ [0.0064, 0.1538];  >1.0 判为量纲错误 → 拒用 +
     显式告警, 绝不让门静默恒开/恒关 (静默死门教训);
  4. 快照是盘前观测: 不当实时行情用, 命中兜底必须留"依据 + 快照龄"痕迹;
  5. 门关着 (low_atr_gate_enabled=false) 时快照一律不读 —— 不新增硬门,
     单旋钮同开同关;
  6. 遥测 reason 仍是 `low_atr_block` (尺子连续性: early_acceptance_check.py:172 /
     first_night_gate_report.py:101 按 reason 计数), 依据只进**新增**键。
"""
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.playbook.pipeline import (  # noqa: E402
    _check_intraday_trigger, _screening_age_hours,
)

_SRC = (Path(__file__).resolve().parents[2]
        / "app" / "playbook" / "pipeline_parts" / "trigger_flow.py"
        ).read_text(encoding="utf-8")

# 与 tests/test_playbook_pipeline.py::TestCheckIntradayTrigger 同款密封档,
# 只把低ATR门打开 (本文件测的就是它)。
CFG = {
    "entry_time_cutoff_enabled": False,
    # 2026-09-14: 同 test_playbook_pipeline.py 一并补密封 — 开盘禁触窗是 wall-clock 门
    #   (yaml true/0935), 不密封则本文件在 ET 00:00-09:35 随挂钟漂移。
    "entry_open_blackout_enabled": False,
    "earnings_blackout_gate_enabled": False,
    "low_atr_gate_enabled": True,
    "low_atr_gate_min_atr_pct": 0.03,       # 比例口径: 3%
    "symbol_streak_ban_enabled": False,
    "intraday_vol_gate_enabled": False,
    "entry_vwap_gate_enabled": False,
    "entry_reanchor_enabled": True,
    "entry_reanchor_max_drift_pct": 4.0,
    "thesis_drift_invalidate_enabled": True,
    "falsification_gates_enabled": False,
}


@pytest.fixture(autouse=True)
def _sealed_pb_cfg(monkeypatch):
    monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: dict(CFG))


@pytest.fixture(autouse=True)
def _reset_low_atr_log_dedup():
    """`_low_atr_logged` 是模块级 (symbol, ET日) 去重集 — 不清会让先跑的用例
    吃掉后跑用例的日志行 (当日首记后静默), 断言随执行顺序漂。"""
    from app.playbook import pipeline as pipe
    pipe._low_atr_logged.clear()
    yield
    pipe._low_atr_logged.clear()


def scheduler(price=99.0):
    """BUY entry=100 / price=99 ⇒ 触发带内 (低ATR门之外无其他拦截)。"""
    sch = MagicMock()
    sch.data_router.get_full_market_context.return_value = {
        "price": price, "day_high": 101.0, "day_low": 98.0,
        "volume_5d_avg_ratio": 2.0,
    }
    return sch


def inst(**over):
    base = {"symbol": "US.TESTA", "direction": "BUY", "entry_price": 100.0}
    base.update(over)
    return base


def screening(ratio, hours_ago=2.0, **extra):
    sc = {"screening_atr_ratio": ratio,
          "observed_at": (datetime.now().astimezone()
                          - timedelta(hours=hours_ago)).isoformat()}
    sc.update(extra)
    return sc


class TestPrimaryBasisUnchanged:
    def test_row_atr_pct_below_threshold_blocks(self, caplog):
        with caplog.at_level(logging.INFO):
            assert _check_intraday_trigger(scheduler(), inst(atr_pct=0.02)) is False
        assert "basis=instruction_atr_pct" in caplog.text

    def test_row_atr_pct_above_threshold_triggers(self):
        assert _check_intraday_trigger(scheduler(), inst(atr_pct=0.05)) is True

    def test_row_atr_pct_wins_over_screening(self):
        """主口径优先: 行内 atr_pct 合格时不因快照低而拦 (不抢口径, 不收紧)。"""
        row = inst(atr_pct=0.05,
                   raw_instruction={"source": "universe_expansion",
                                    "candidate_screening": screening(0.01)})
        assert _check_intraday_trigger(scheduler(), row) is True


class TestScreeningFallback:
    def test_missing_atr_pct_with_low_screening_blocks(self, caplog):
        """修前: atr_pct 缺失 ⇒ fail-open 恒开。修后: 读同义字段 ⇒ 拦。"""
        row = inst(raw_instruction={"source": "universe_expansion",
                                    "candidate_screening": screening(0.02)})
        with caplog.at_level(logging.INFO):
            assert _check_intraday_trigger(scheduler(), row) is False
        assert "basis=candidate_screening" in caplog.text
        assert "快照龄=" in caplog.text          # 盘前快照必须带龄, 不冒充实时

    def test_log_dedup_does_not_disable_gate(self, caplog):
        """当日首记后日志静默, 但**拦截照常** (去重只作用于噪声, 不作用于门)。"""
        row = inst(raw_instruction={"candidate_screening": screening(0.02)})
        with caplog.at_level(logging.INFO):
            assert _check_intraday_trigger(scheduler(), row) is False
            caplog.clear()
            assert _check_intraday_trigger(scheduler(), row) is False
        assert "低ATR门拦截" not in caplog.text

    def test_raw_instruction_json_string_roundtrip(self):
        """DB 回读形态 (str) 与 dict 形态都要能读 — 2026-09-01 TypeError 教训。"""
        row = inst(raw_instruction_json=json.dumps(
            {"source": "universe_expansion", "candidate_screening": screening(0.02)}))
        assert _check_intraday_trigger(scheduler(), row) is False

    def test_missing_atr_pct_with_healthy_screening_triggers(self):
        row = inst(raw_instruction={"candidate_screening": screening(0.05)})
        assert _check_intraday_trigger(scheduler(), row) is True

    def test_no_atr_and_no_screening_still_fail_open(self):
        """两路都无数 ⇒ 保持既有 fail-open (缺数 ≠ 拒绝, 不新增硬门)。"""
        assert _check_intraday_trigger(scheduler(), inst()) is True

    @pytest.mark.parametrize("bad", [0, 0.0, -0.01, "abc", None])
    def test_unusable_row_value_falls_back_to_screening(self, bad):
        row = inst(atr_pct=bad,
                   raw_instruction={"candidate_screening": screening(0.02)})
        assert _check_intraday_trigger(scheduler(), row) is False


class TestUnitDiscipline:
    """量纲军规 (AGENT.md 2026-09-02): 单位混装已四案, 新字段必须钉死。"""

    def test_threshold_is_fraction_not_percent(self):
        """0.03 = 3%: 4% 合格 / 2.9% 拦截。若有人写成 3.0 则门恒拦。"""
        assert _check_intraday_trigger(scheduler(), inst(atr_pct=0.04)) is True
        assert _check_intraday_trigger(scheduler(), inst(atr_pct=0.029)) is False

    def test_screening_ratio_above_one_is_rejected_as_unit_error(self, caplog):
        """快照写成百分数 (3.0) ⇒ 拒用 + 告警 + fail-open, 不静默把门变成恒开/恒拦。"""
        row = inst(raw_instruction={"candidate_screening": screening(3.0)})
        with caplog.at_level(logging.WARNING):
            assert _check_intraday_trigger(scheduler(), row) is True
        assert "量纲错误" in caplog.text

    def test_screening_ratio_at_threshold_triggers(self):
        """`<` 才拦 ⇒ 恰好 0.03 放行 (与 evaluate_candidate 同边界口径)。"""
        row = inst(raw_instruction={"candidate_screening": screening(0.03)})
        assert _check_intraday_trigger(scheduler(), row) is True


class TestKnobDiscipline:
    def test_gate_disabled_never_reads_screening(self, monkeypatch):
        cfg = dict(CFG, low_atr_gate_enabled=False)
        monkeypatch.setattr("app.playbook.config.get_playbook_config", lambda: cfg)
        row = inst(atr_pct=0.001,
                   raw_instruction={"candidate_screening": screening(0.001)})
        assert _check_intraday_trigger(scheduler(), row) is True


class TestScreeningAgeHours:
    def test_aware_timestamp_returns_hours(self):
        stamp = (datetime.now().astimezone() - timedelta(hours=3)).isoformat()
        assert 2.9 < _screening_age_hours(stamp) < 3.1

    def test_naive_timestamp_returns_none(self):
        """naive 不猜时区 (to_et 把 naive 当北京时间 ⇒ 移半天)。"""
        assert _screening_age_hours(datetime(2026, 9, 11, 8, 0).isoformat()) is None

    @pytest.mark.parametrize("bad", [None, "", "not-a-time", 12345, {}, []])
    def test_unparsable_returns_none(self, bad):
        assert _screening_age_hours(bad) is None

    def test_future_stamp_is_negative_not_clamped(self):
        """夹零会把时钟/时区错误伪装成"刚观测"。"""
        stamp = (datetime.now().astimezone() + timedelta(hours=2)).isoformat()
        assert _screening_age_hours(stamp) < 0

    def test_zulu_suffix_accepted(self):
        stamp = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        age = _screening_age_hours(stamp)
        assert age is not None and 0.9 < age < 1.1


class TestTelemetryContract:
    """源级契约: 尺子连续性 (reason 取值不变) + 新键只增不改。"""

    def test_reason_string_unchanged(self):
        gate = _SRC[_SRC.index("if _atr_gate_enabled:"):]
        gate = gate[:gate.index("#   连败封禁")]
        assert '_emit_trigger_miss("low_atr_block", current_price, extra={' in gate

    def test_extra_declares_basis_value_and_threshold(self):
        gate = _SRC[_SRC.index("if _atr_gate_enabled:"):]
        for key in ('"atr_basis"', '"atr_value"', '"atr_threshold"', '"atr_age_hours"'):
            assert key in gate, key

    def test_emit_trigger_miss_extra_is_additive(self):
        """extra 只 update 新键; 既有键 (ts/date/reason/…) 不被覆盖。"""
        assert "if isinstance(extra, dict):" in _SRC
        assert "_row.update({k: v for k, v in extra.items() if v is not None})" in _SRC
